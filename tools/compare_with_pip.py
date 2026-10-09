"""Compare an application built by pyxbuild with ``pip install`` on real Windows.

    python tools/compare_with_pip.py APP.zip PYLOCK.toml [EXCLUDE-PATTERN ...]

Run on Windows with the same Python minor version as the app. Installs the
exact wheels from the lock file with pip into a fresh venv and compares the
installed file trees (paths and RECORD entries) with the app's
``runtime/Lib/site-packages``. Files pip and pyxbuild legitimately write
differently (INSTALLER, REQUESTED, direct_url.json, script launchers,
bytecode) are ignored. The project's own wheel is not in the lock and is
skipped.
"""

from __future__ import annotations

import csv
import io
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

IGNORED_NAMES = {"INSTALLER", "REQUESTED", "direct_url.json", "RECORD"}


def tree(site: Path, dists: set[str]) -> dict[str, set[str]]:
    """{dist-info dir: set of RECORD paths}, restricted to ``dists``."""
    out = {}
    for record in site.glob("*.dist-info/RECORD"):
        name = record.parent.name.split("-")[0].lower()
        if name not in dists:
            continue
        rows = csv.reader(io.StringIO(record.read_text(encoding="utf-8")))
        paths = set()
        for row in rows:
            if not row:
                continue
            p = row[0].replace("\\", "/")
            if p.rsplit("/", 1)[-1] in IGNORED_NAMES or "__pycache__" in p:
                continue
            if "/Scripts/" in p or p.startswith("../../Scripts"):
                p = "SCRIPTS/" + p.rsplit("/", 1)[-1].lower()
            paths.add(p)
        out[record.parent.name] = paths
    return out


def main() -> int:
    app_zip, lock_path = Path(sys.argv[1]), Path(sys.argv[2])
    # files the project excluded on purpose ([tool.pyxbuild] exclude)
    excludes = sys.argv[3:]
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from pyxbuild.lock import read_lock, select_files
    from pyxbuild.targets import WIN_AMD64

    py = lock["tool"]["pyxbuild"]["python"]
    minor = ".".join(py.split(".")[:2])
    env = WIN_AMD64.marker_environment(py)
    selections = select_files(read_lock(lock_path), WIN_AMD64, env, WIN_AMD64.tags(minor))
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        with zipfile.ZipFile(app_zip) as zf:
            zf.extractall(tmp / "app")
        (app_dir,) = (tmp / "app").iterdir()
        venv = tmp / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        pip = [str(venv / "Scripts" / "python.exe"), "-m", "pip"]
        reqs = tmp / "requirements.txt"
        reqs.write_text("".join(f"{s.file.url} --hash=sha256:{s.file.sha256}\n"
                                for s in selections))
        subprocess.run([*pip, "install", "--no-deps", "--no-compile", "--require-hashes",
                        "-r", str(reqs)], check=True)
        dists = {s.package.name.replace("-", "_").lower() for s in selections}
        ours = tree(app_dir / "runtime" / "Lib" / "site-packages", dists)
        theirs = tree(venv / "Lib" / "site-packages", dists)
    from pyxbuild.install import Excluder

    excluder = Excluder(excludes)
    theirs = {d: {p for p in paths if not excluder.matches(p)} for d, paths in theirs.items()}
    problems = []
    for dist in sorted(set(ours) | set(theirs)):
        if dist not in ours or dist not in theirs:
            problems.append(f"{dist}: only in {'pyxbuild' if dist in ours else 'pip'}")
            continue
        for p in sorted(ours[dist] ^ theirs[dist]):
            side = "pyxbuild" if p in ours[dist] else "pip"
            problems.append(f"{dist}: {p} only in {side}")
    if problems:
        print("\n".join(problems))
        return 1
    print(f"identical file trees for {len(ours)} distributions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
