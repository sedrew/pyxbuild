"""Build and run the golden projects (tests/golden) outside pytest, for CI.

    python tools/golden.py build --out out/            # build all, write zips + SHA256SUMS
    python tools/golden.py run out/                    # unzip and run every app (Windows/Wine)

The build host and the run host differ in CI on purpose: apps built on
Linux and macOS are run on windows-latest.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden"

# (project, entry point, python versions)
MATRIX = [
    ("hello", "hello", ["3.12"]),
    ("features", "features", ["3.11", "3.12", "3.13", "3.14"]),
    ("level1", "level1", ["3.11", "3.12", "3.13", "3.14"]),
    ("level2", "level2", ["3.12", "3.13"]),
    ("level3", "level3", ["3.12"]),  # torch: CI only, Wine lacks NUMA APIs OpenMP needs
]


def build(out: Path, only: set[str]) -> int:
    out.mkdir(parents=True, exist_ok=True)
    sums = []
    for project, _, pythons in MATRIX:
        if only and project not in only:
            continue
        for py in pythons:
            with tempfile.TemporaryDirectory() as tmp:
                proj = Path(tmp) / project
                shutil.copytree(GOLDEN / project, proj,
                                ignore=shutil.ignore_patterns("dist", "pylock.*.toml"))
                cmd = [sys.executable, "-m", "pyxbuild", "build", str(proj), "--python", py,
                       "--zip", "--out", str(Path(tmp) / "dist")]
                print("+", " ".join(cmd), flush=True)
                subprocess.run(cmd, check=True)
                (zip_path,) = (Path(tmp) / "dist").glob("*.zip")
                dest = out / f"{project}-py{py}.zip"
                shutil.copyfile(zip_path, dest)
                shutil.copyfile(proj / "pylock.win_amd64.toml",
                                out / f"{project}-py{py}.pylock.toml")
                digest = hashlib.sha256(dest.read_bytes()).hexdigest()
                sums.append(f"{digest}  {dest.name}")
                # per-file digests, so a mismatch between hosts names the files
                with zipfile.ZipFile(dest) as zf:
                    lines = [f"{hashlib.sha256(zf.read(i)).hexdigest()}  {i.filename}"
                             for i in zf.infolist()]
                (out / f"{project}-py{py}.files.txt").write_text("\n".join(lines) + "\n",
                                                                  newline="\n")
                print(sums[-1], flush=True)
    (out / "SHA256SUMS").write_text("\n".join(sums) + "\n", newline="\n")
    return 0


def run(out: Path, only: set[str]) -> int:
    failures = []
    entries = {p: e for p, e, _ in MATRIX}
    for zip_path in sorted(out.glob("*.zip")):
        project = zip_path.name.split("-py")[0]
        if only and project not in only:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(tmp)
            (app_dir,) = Path(tmp).iterdir()
            exe = app_dir / f"{entries[project]}.exe"
            cmd = [str(exe)]
            if sys.platform != "win32":
                cmd.insert(0, os.environ.get("PYXBUILD_WINE", "wine"))
            print(f"== {zip_path.name}", flush=True)
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=1800)
            print(proc.stdout)
            if proc.returncode != 0 or "FAIL" in proc.stdout:
                print(proc.stderr)
                failures.append(zip_path.name)
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    print("all golden apps passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["build", "run"])
    parser.add_argument("out", type=Path, nargs="?", default=Path("golden-out"))
    parser.add_argument("--out", dest="out_opt", type=Path)
    parser.add_argument("--only", action="append", default=[])
    args = parser.parse_args()
    out = args.out_opt or args.out
    return (build if args.command == "build" else run)(out, set(args.only))


if __name__ == "__main__":
    sys.exit(main())
