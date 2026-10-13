"""Cross-build compatibility matrix for packages with compiled extensions.

For every (package, Python version) pair: generate a one-dependency project,
build it with pyxbuild on this host, run its smoke test on Windows (natively
or under $PYXBUILD_WINE) and record the outcome in a Markdown table.

    python tools/compat_matrix.py --jobs 4 --out docs/COMPATIBILITY.md
    python tools/compat_matrix.py --packages numpy,pandas --pythons 3.12
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import platform
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# requirement -> smoke test (runs inside the built app on Windows)
PACKAGES: dict[str, str] = {
    "numpy": "import numpy as np; assert np.linalg.inv(np.eye(3)).sum() == 3",
    "pandas": "import pandas as pd; assert pd.DataFrame({'a':[1,2]}).a.sum() == 3",
    "Pillow": "from PIL import Image; import io; b=io.BytesIO(); "
              "Image.new('RGB',(4,4)).save(b,'WEBP'); assert b.tell()",
    "lxml": "from lxml import etree; "
            "assert etree.fromstring(b'<a><b/></a>').xpath('count(//b)')==1",
    "cryptography": "from cryptography.fernet import Fernet; k=Fernet(Fernet.generate_key()); "
                    "assert k.decrypt(k.encrypt(b'x'))==b'x'",
    "pydantic": "import pydantic; assert pydantic.TypeAdapter(int).validate_python('3')==3",
    "scipy": "from scipy import linalg; import numpy as np; assert linalg.det(np.eye(2))==1",
    "matplotlib": "import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as p; "
                  "import io; f=p.figure(); f.savefig(io.BytesIO(), format='png')",
    "scikit-learn": "from sklearn.linear_model import LinearRegression; "
                    "assert LinearRegression().fit([[0],[1]],[0,1]).predict([[2]])[0] > 1.9",
    "pyarrow": "import pyarrow as pa; assert pa.array([1,2]).sum().as_py()==3",
    "polars": "import polars as pl; assert pl.DataFrame({'a':[1,2]})['a'].sum()==3",
    "duckdb": "import duckdb; assert duckdb.sql('select 42').fetchone()[0]==42",
    "psutil": "import psutil, os; assert psutil.Process(os.getpid()).pid == os.getpid()",
    "orjson": "import orjson; assert orjson.loads(orjson.dumps([1]))==[1]",
    "msgpack": "import msgpack; assert msgpack.unpackb(msgpack.packb([1]))==[1]",
    "pyzmq": "import zmq; assert zmq.zmq_version()",
    "opencv-python-headless": "import cv2, numpy as np; "
                              "assert cv2.imencode('.png', np.zeros((2,2),np.uint8))[0]",
    "SQLAlchemy": "import sqlalchemy as sa; e=sa.create_engine('sqlite://'); "
                  "assert e.connect().execute(sa.text('select 1')).scalar()==1",
    "PyYAML": "import yaml; assert yaml.safe_load('a: 1')=={'a':1}; assert yaml.__with_libyaml__",
    "regex": "import regex; assert regex.match(r'\\p{L}+', 'abc')",
    "aiohttp": "import aiohttp, asyncio; asyncio.run(asyncio.sleep(0))",
    "grpcio": "import grpc; assert grpc.__version__",
    "shapely": "from shapely.geometry import Point; assert Point(0,0).buffer(1).area > 3",
    "h5py": "import h5py, io; f=h5py.File(io.BytesIO(),'w'); f['x']=[1,2]; assert f['x'][1]==2",
    "numba": "from numba import njit; assert njit(lambda x: x+1)(1)==2",
    "pyproj": "from pyproj import Transformer; "
              "t=Transformer.from_crs(4326,3857,always_xy=True); assert t.transform(0,0)[0]==0",
    "bcrypt": "import bcrypt; h=bcrypt.hashpw(b'x', bcrypt.gensalt(4)); "
              "assert bcrypt.checkpw(b'x', h)",
    "zstandard": "import zstandard as z; "
                 "assert z.decompress(z.compress(b'x'*10))==b'x'*10",
    "pycryptodome": "from Crypto.Cipher import AES; assert AES.new(b'k'*16, AES.MODE_ECB)",
    "PyMuPDF": "import pymupdf; d=pymupdf.open(); d.new_page(); assert d.page_count==1",
    "pywin32": "import win32api, pythoncom; assert win32api.GetVersion()",
    "PySide6-Essentials": "from PySide6.QtCore import QCoreApplication; "
                          "assert QCoreApplication([]).applicationName() is not None",
    "onnxruntime": "import onnxruntime as o; assert 'CPUExecutionProvider' in "
                   "o.get_available_providers()",
    "tokenizers": "from tokenizers import Tokenizer; from tokenizers.models import BPE; "
                  "assert Tokenizer(BPE())",
    "statsmodels": "import statsmodels.api as sm; "
                   "assert abs(sm.OLS([1,2],[1,2]).fit().params[0]-1) < 1e-9",
    "torch": "import torch; assert (torch.ones(2,2) @ torch.ones(2,2)).sum().item()==8",
    "torch + msvc-runtime": "import torch; "
                            "assert (torch.ones(2,2) @ torch.ones(2,2)).sum().item()==8",
    "uvloop": "import uvloop",  # no Windows support: expected to fail resolution
}


@dataclass
class Cell:
    package: str
    python: str
    status: str  # ok | no-wheel | build-error | run-error
    detail: str = ""
    seconds: float = 0.0
    size_mb: float = 0.0
    version: str = ""


def _project(dest: Path, requirement: str, smoke: str, python: str) -> Path:
    proj = dest / "app"
    (proj / "smokeapp").mkdir(parents=True)
    (proj / "pyproject.toml").write_text(textwrap.dedent(f"""
        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"
        [project]
        name = "smokeapp"
        version = "1.0"
        dependencies = {json.dumps(requirement.split(" + "))}
        [project.scripts]
        smokeapp = "smokeapp:main"
        [tool.pyxbuild]
        python = "{python}"
    """))
    (proj / "smokeapp" / "__init__.py").write_text(textwrap.dedent(f"""
        import os, sys
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        def main():
            {smoke}
            from importlib.metadata import version
            print("SMOKE-OK", version({json.dumps(requirement.split(" + ")[0])}))
            return 0
    """))
    return proj


def _run_cell(package: str, python: str, smoke: str) -> Cell:
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="pyxb-matrix-") as tmp:
        proj = _project(Path(tmp), package, smoke, python)
        build = subprocess.run([sys.executable, "-m", "pyxbuild", "build", str(proj), "-q"],
                               capture_output=True, text=True)
        if build.returncode != 0:
            err = build.stderr.strip()
            status = "no-wheel" if ("no wheel" in err or "has no wheels" in err
                                    or "only-binary" in err or "no usable wheels" in err
                                    or "only an sdist" in err) else "build-error"
            return Cell(package, python, status, _short(err), time.monotonic() - start)
        app = proj / "dist" / "smokeapp-win_amd64"
        size = sum(p.stat().st_size for p in app.rglob("*") if p.is_file()) / 2**20
        cmd = [str(app / "smokeapp.exe")]
        if sys.platform != "win32":
            cmd.insert(0, os.environ.get("PYXBUILD_WINE", "wine"))
        env = {**os.environ, "WINEDEBUG": "-all"}
        try:
            run = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=env,
                                 encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return Cell(package, python, "run-error", "timeout", time.monotonic() - start, size)
        out = run.stdout.strip()
        if run.returncode == 0 and "SMOKE-OK" in out:
            version = out.split("SMOKE-OK", 1)[1].strip().split()[0]
            return Cell(package, python, "ok", "", time.monotonic() - start, size, version)
        return Cell(package, python, "run-error", _short(run.stderr or out),
                    time.monotonic() - start, size)


def _short(text: str, limit: int = 160) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    interesting = [ln for ln in lines if any(k in ln for k in (
        "Error", "error", "no wheel", "has no wheels", "FAIL", "not found", "unimplemented"))]
    msg = (interesting[-1] if interesting else (lines[-1] if lines else "")).replace("|", "/")
    return msg[:limit]


ICON = {"ok": "✅", "no-wheel": "⛔ no wheel", "build-error": "❌ build", "run-error": "⚠️ run"}


def render(cells: list[Cell], pythons: list[str], runner: str) -> str:
    by = {(c.package, c.python): c for c in cells}
    head = "| package | " + " | ".join(pythons) + " |"
    lines = [
        "# Compatibility matrix",
        "",
        f"Generated by `tools/compat_matrix.py` on {time.strftime('%Y-%m-%d')}: built on "
        f"{platform.system()} {platform.machine()}, run on {runner}. Each cell builds a "
        "project depending on that one package and runs a smoke test in the built app.",
        "",
        "✅ works · ⛔ no win_amd64 wheel for that Python · ❌ build failed · "
        "⚠️ built, smoke test failed",
        "",
        head,
        "|---|" + "---|" * len(pythons),
    ]
    for pkg in PACKAGES:
        if not any((pkg, py) in by for py in pythons):
            continue
        row = []
        for py in pythons:
            c = by.get((pkg, py))
            if c is None:
                row.append("")
            elif c.status == "ok":
                row.append(f"✅ {c.version} ({c.size_mb:.0f} MB)")
            else:
                row.append(ICON[c.status])
        lines.append(f"| {pkg} | " + " | ".join(row) + " |")
    problems = [c for c in cells if c.status != "ok"]
    if problems:
        lines += ["", "## Details", "", "| package | python | result | message |",
                  "|---|---|---|---|"]
        for c in sorted(problems, key=lambda c: (c.package.lower(), c.python)):
            lines.append(f"| {c.package} | {c.python} | {c.status} | `{c.detail}` |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packages", default=",".join(PACKAGES))
    parser.add_argument("--pythons", default="3.11,3.12,3.13,3.14")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--json", type=Path, help="also write raw results as JSON")
    parser.add_argument("--runner", default="Windows" if sys.platform == "win32" else "Wine")
    args = parser.parse_args()
    packages = [p for p in args.packages.split(",") if p]
    unknown = [p for p in packages if p not in PACKAGES]
    if unknown:
        parser.error(f"no smoke test for: {', '.join(unknown)}")
    pythons = args.pythons.split(",")
    cells: list[Cell] = []
    with cf.ThreadPoolExecutor(args.jobs) as pool:
        futures = {pool.submit(_run_cell, p, py, PACKAGES[p]): (p, py)
                   for p in packages for py in pythons}
        for fut in cf.as_completed(futures):
            p, py = futures[fut]
            try:
                cell = fut.result()
            except Exception as exc:  # keep going; report the crash in the table
                cell = Cell(p, py, "build-error", _short(repr(exc)))
            cells.append(cell)
            print(f"{cell.status:12} {p:24} {py}  {cell.seconds:5.0f}s  {cell.detail}", flush=True)
    text = render(cells, pythons, args.runner)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text)
    if args.json:
        args.json.write_text(json.dumps([asdict(c) for c in cells], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
