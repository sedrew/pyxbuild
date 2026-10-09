"""Benchmarks: build time, bundle size and start-up time (optionally vs PyInstaller).

    python tools/bench.py --project hello --python 3.12 --json bench.json
    python tools/bench.py --project level1 --pyinstaller      # Windows host only

Build scenarios
    cold    empty download cache, no lock file (download + resolve + install)
    warm    cache filled, no lock file (resolve + install)
    frozen  cache filled, lock file present (install only)
Variants (size and start-up)
    default        pyxbuild defaults
    slim-tk        slim = default + ["tk", "pydoc"]
    bytecode       bytecode = true (precompiled .pyc)
Start-up is measured by running the app's entry point with ``--exit=0``-like
arguments N times; the first run after a build is reported separately
because Python writes __pycache__ then. On Linux/macOS the app runs under
$PYXBUILD_WINE, so absolute numbers include Wine overhead.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden"
ENTRY = {"hello": ("hello", ["--exit=0"]), "level1": ("level1", ["numpy"]),
         "features": ("features", ["--exit", "0"])}

VARIANTS = {
    "default": "",
    "slim-tk": 'slim = ["tests", "idle", "ensurepip", "turtledemo", "lib2to3", "tk", "pydoc"]\n',
    "bytecode": "bytecode = true\n",
}


def _tool_table(proj: Path, extra: str) -> None:
    text = (proj / "pyproject.toml").read_text()
    if "[tool.pyxbuild]" in text:
        text = text.replace("[tool.pyxbuild]\n", "[tool.pyxbuild]\n" + extra, 1)
    else:
        text += "\n[tool.pyxbuild]\n" + extra
    (proj / "pyproject.toml").write_text(text)


def _build(proj: Path, python: str, cache: Path, *extra: str) -> float:
    env = {**os.environ, "PYXBUILD_CACHE_DIR": str(cache)}
    start = time.perf_counter()
    subprocess.run([sys.executable, "-m", "pyxbuild", "build", str(proj), "-q", "--zip",
                    "--python", python, *extra], check=True, env=env,
                   stdout=subprocess.DEVNULL)
    return time.perf_counter() - start


def _size(app: Path) -> dict:
    files = [p for p in app.rglob("*") if p.is_file()]
    zips = list(app.parent.glob("*.zip"))
    return {"bytes": sum(p.stat().st_size for p in files), "files": len(files),
            "zip_bytes": zips[0].stat().st_size if zips else None}


def _run(exe: Path, args: list[str]) -> float:
    cmd = [str(exe), *args]
    if sys.platform != "win32":
        cmd.insert(0, os.environ.get("PYXBUILD_WINE", "wine"))
    env = {**os.environ, "WINEDEBUG": "-all"}
    start = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, env=env)
    elapsed = time.perf_counter() - start
    if proc.returncode != 0:
        raise RuntimeError(f"{exe.name} failed: {proc.stderr.decode(errors='replace')[-500:]}")
    return elapsed


def _startup(exe: Path, args: list[str], runs: int) -> dict:
    first = _run(exe, args)
    times = [_run(exe, args) for _ in range(runs)]
    return {"first_s": round(first, 3), "median_s": round(statistics.median(times), 3),
            "min_s": round(min(times), 3)}


def bench_pyxbuild(project: str, python: str, runs: int, work: Path, cache: Path) -> dict:
    out: dict = {"tool": "pyxbuild", "project": project, "python": python, "builds": {},
                 "variants": {}}
    entry, args = ENTRY[project]
    # build-time scenarios
    cold_cache = work / "cold-cache"
    proj = work / "build-scenarios" / project
    shutil.copytree(GOLDEN / project, proj, ignore=shutil.ignore_patterns("dist", "pylock.*"))
    out["builds"]["cold"] = round(_build(proj, python, cold_cache), 2)
    (proj / "pylock.win_amd64.toml").unlink()
    out["builds"]["warm"] = round(_build(proj, python, cold_cache), 2)
    out["builds"]["frozen"] = round(_build(proj, python, cold_cache, "--frozen"), 2)
    for name, extra in VARIANTS.items():
        vproj = work / name / project
        shutil.copytree(GOLDEN / project, vproj,
                        ignore=shutil.ignore_patterns("dist", "pylock.*"))
        if extra:
            _tool_table(vproj, extra)
        seconds = _build(vproj, python, cache)
        (app,) = [d for d in (vproj / "dist").iterdir() if d.is_dir()]
        info = {"build_s": round(seconds, 2), **_size(app)}
        info.update(_startup(app / f"{entry}.exe", args, runs))
        out["variants"][name] = info
        if name == "default":
            # floor: the bundled interpreter doing nothing (process creation,
            # interpreter start-up; under Wine also Wine's own overhead)
            base = [_run(app / "runtime" / "python.exe", ["-I", "-c", "pass"])
                    for _ in range(runs)]
            out["python_noop_median_s"] = round(statistics.median(base), 3)
    return out


def bench_pyinstaller(project: str, runs: int, work: Path) -> dict:
    """Same project with PyInstaller --onedir; only meaningful on a Windows host."""
    entry, args = ENTRY[project]
    src = GOLDEN / project
    venv = work / "pyi-venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    subprocess.run([str(py), "-m", "pip", "install", "-q", "pyinstaller", str(src)], check=True)
    module = next(line.split("=", 1)[1].strip().strip('"') for line in
                  (src / "pyproject.toml").read_text().splitlines()
                  if line.startswith(f"{entry} ="))
    mod, func = module.split(":")
    script = work / "pyi_entry.py"
    script.write_text(f"import sys\nfrom {mod} import {func}\nsys.exit({func}())\n")
    start = time.perf_counter()
    subprocess.run([str(py), "-m", "PyInstaller", "--noconfirm", "--onedir", "--name", entry,
                    "--distpath", str(work / "pyi-dist"), "--workpath", str(work / "pyi-build"),
                    "--specpath", str(work), "--collect-all", "certifi", str(script)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    seconds = time.perf_counter() - start
    app = work / "pyi-dist" / entry
    zpath = work / "pyi-dist" / f"{entry}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in sorted(app.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(app.parent).as_posix())
    info = {"build_s": round(seconds, 2), **_size(app), "zip_bytes": zpath.stat().st_size}
    info.update(_startup(app / f"{entry}.exe", args, runs))
    return {"tool": "pyinstaller", "project": project, "variants": {"onedir": info}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="hello", choices=sorted(ENTRY))
    parser.add_argument("--python", default="3.12")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--pyinstaller", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    results = []
    wine = os.environ.get("PYXBUILD_WINE")
    if sys.platform != "win32" and wine:
        # keep one wineserver alive so each run does not pay its start-up
        subprocess.Popen([str(Path(wine).with_name("wineserver")), "-p", "-f"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        time.sleep(1)
    with tempfile.TemporaryDirectory(prefix="pyxbuild-bench-") as tmp:
        work = Path(tmp)
        cache = Path(os.environ.get("PYXBUILD_CACHE_DIR") or work / "cache")
        results.append(bench_pyxbuild(args.project, args.python, args.runs, work, cache))
        if args.pyinstaller:
            results.append(bench_pyinstaller(args.project, args.runs, work))
    text = json.dumps(results, indent=1)
    print(text)
    if args.json:
        args.json.write_text(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
