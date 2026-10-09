"""Try the experimental zig cross-compilation on real sdists from PyPI.

For each package: download the latest sdist, cross-build a win_amd64 wheel
on this host, install it into a copy of the target runtime and run a smoke
test with the Windows interpreter (natively or under $PYXBUILD_WINE).

    python tools/cross_matrix.py --python 3.12 --out docs/CROSS_COMPILE.md
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from pyxbuild.crossbuild import cross_build_wheel
from pyxbuild.download import fetch
from pyxbuild.errors import PyxbuildError
from pyxbuild.install import install_wheel
from pyxbuild.runtime import get_runtime
from pyxbuild.targets import WIN_AMD64

# package -> (smoke test, expectation note)
PACKAGES = {
    "markupsafe": ("import markupsafe, markupsafe._speedups; "
                   "assert str(markupsafe.escape('<a>'))=='&lt;a&gt;'", "C, setuptools"),
    "msgpack": ("import msgpack._cmsgpack, msgpack; "
                "assert msgpack.unpackb(msgpack.packb([1]))==[1]", "Cython + C++"),
    "simplejson": ("import simplejson, simplejson._speedups; "
                   "assert simplejson.loads('[1]')==[1]", "C"),
    "bitarray": ("from bitarray import bitarray; assert bitarray('101').count()==2", "C"),
    "pyyaml": ("import yaml; assert yaml.safe_load('a: 1')=={'a':1}", "Cython, optional libyaml"),
    "regex": ("import regex; assert regex.match(r'\\p{L}+','abc')", "C"),
    "zstandard": ("import zstandard as z; assert z.decompress(z.compress(b'x'*9))==b'x'*9",
                  "C + bundled zstd (cffi optional)"),
    "lz4": ("import lz4.frame as f; assert f.decompress(f.compress(b'abc'))==b'abc'",
            "C + bundled lz4"),
    "wrapt": ("import wrapt, wrapt._wrappers; assert wrapt.__version__", "C"),
    "multidict": ("import multidict._multidict; assert multidict.MultiDict(a=1)['a']==1", "C"),
    "frozenlist": ("import frozenlist; assert frozenlist.FrozenList([1])[0]==1", "Cython"),
    "yarl": ("import yarl._quoting_c as q; assert q._Quoter()('a b')=='a%20b'", "Cython"),
    "propcache": ("import propcache; assert propcache.__version__", "Cython"),
    "cytoolz": ("import sys, types; sys.modules['toolz']=types.ModuleType('toolz'); "
                "import cytoolz.itertoolz as i; assert i.first([3])==3", "Cython"),
    "ujson": ("import ujson; assert ujson.loads('[1]')==[1]", "C + C++ (double-conversion)"),
    "levenshtein": ("import Levenshtein; assert Levenshtein.distance('ab','ac')==1",
                    "C++ (scikit-build)"),
    "psutil": ("import psutil; assert psutil.pids()", "platform-specific setup.py"),
    "cffi": ("import cffi; cffi.FFI()", "needs libffi"),
    "numpy": ("import numpy", "meson-python"),
}


def latest_sdist(package: str) -> tuple[str, str, str, str]:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{package}/json", timeout=60) as r:
        data = json.load(r)
    version = data["info"]["version"]
    for f in data["urls"]:
        if f["packagetype"] == "sdist":
            return version, f["url"], f["digests"]["sha256"], f["filename"]
    raise PyxbuildError(f"{package} {version} has no sdist")


def run_cell(package: str, smoke: str, runtime) -> dict:
    start = time.monotonic()
    result = {"package": package, "version": "", "status": "", "detail": ""}
    try:
        version, url, sha, filename = latest_sdist(package)
        result["version"] = version
        sdist = fetch(url, sha, filename, quiet=True)
        with tempfile.TemporaryDirectory(prefix="pyxb-xm-") as tmp:
            tmp = Path(tmp)
            try:
                wheel = cross_build_wheel(sdist, runtime, tmp / "wheels", quiet=True)
            except PyxbuildError as exc:
                result.update(status="build-error", detail=_last(str(exc)))
                return result
            rt = tmp / "rt"
            shutil.copytree(runtime.path, rt)
            for d in sorted((rt / "Lib" / "site-packages").iterdir()):
                if d.name != "README.txt":
                    shutil.rmtree(d) if d.is_dir() else d.unlink()
            install_wheel(wheel.path, rt)
            cmd = [str(rt / "python.exe"), "-c", smoke + "; print('SMOKE-OK')"]
            if sys.platform != "win32":
                cmd.insert(0, os.environ.get("PYXBUILD_WINE", "wine"))
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                                  env={**os.environ, "WINEDEBUG": "-all"})
            if "SMOKE-OK" in proc.stdout:
                result["status"] = "ok"
            else:
                result.update(status="run-error", detail=_last(proc.stderr or proc.stdout))
    except Exception as exc:  # report and continue with the next package
        result.update(status="error", detail=_last(repr(exc)))
    result["seconds"] = round(time.monotonic() - start)
    return result


def _last(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    keep = [ln for ln in lines if "error" in ln.lower() or "Error" in ln]
    return (keep[-1] if keep else (lines[-1] if lines else ""))[:180].replace("|", "/")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default="3.12")
    parser.add_argument("--packages", default=",".join(PACKAGES))
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    runtime = get_runtime(WIN_AMD64, args.python, quiet=True)
    names = args.packages.split(",")
    results = []
    with cf.ThreadPoolExecutor(args.jobs) as pool:
        futs = {pool.submit(run_cell, n, PACKAGES[n][0], runtime): n for n in names}
        for fut in cf.as_completed(futs):
            r = fut.result()
            r["kind"] = PACKAGES[r["package"]][1]
            results.append(r)
            print(f"{r['status']:12} {r['package']:14} {r['version']:12} {r['detail']}",
                  flush=True)
    if args.json:
        args.json.write_text(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
