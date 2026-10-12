"""GUI applications (pythonw.exe) with tkinter and multiprocessing, in folder
and onefile mode: the case where PyInstaller apps need freeze_support() and
where sys.stdout is None in every process."""

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from conftest import run_exe, wine_available
from pyxbuild import cli

pytestmark = [pytest.mark.slow, pytest.mark.network,
              pytest.mark.skipif(not wine_available(), reason="needs Windows or Wine")]

ON_WINDOWS = sys.platform == "win32"

APP = '''
import concurrent.futures
import json
import multiprocessing
import os
import sys


def square(x):
    return x * x


def chatty(x):
    print("print from worker", x)             # print() tolerates sys.stdout = None
    sys.stdout.write(f"stdout from worker {x}\\n")  # a plain write does not
    sys.stderr.write(f"stderr from worker {x}\\n")
    sys.stdout.flush()
    return os.getpid()


def boom(x):
    raise ValueError(f"worker failed on {x}")


def work(results):
    with multiprocessing.Pool(2) as pool:
        results["pool"] = sum(pool.map(square, range(10)))
        try:
            results["chatty_pids"] = len(set(pool.map(chatty, range(4))))
        except Exception as exc:
            results["chatty_error"] = f"{type(exc).__name__}: {exc}"
        try:
            pool.apply(boom, (7,))
        except ValueError as exc:
            results["worker_exception"] = str(exc)
    with concurrent.futures.ProcessPoolExecutor(2) as ex:
        results["executor"] = sum(ex.map(square, range(5)))
    results["parent_executable"] = os.path.basename(sys.executable)
    results["log"] = getattr(sys.stdout, "name", None)


def main():
    out = sys.argv[1]
    results = {"argv": sys.argv[1:]}
    try:
        import tkinter
        try:
            root = tkinter.Tk()
        except tkinter.TclError as exc:  # no display (Wine on a headless machine)
            results["tk"] = f"no display: {exc}"
            work(results)
        else:
            tkinter.Label(root, text="pyxbuild").pack()

            def run():
                try:
                    work(results)
                finally:
                    root.destroy()

            root.after(100, run)  # multiprocessing started from the Tk main loop
            root.mainloop()
            results["tk"] = "window"
    except Exception as exc:
        results["error"] = f"{type(exc).__name__}: {exc}"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f)
'''


def _project(root: Path, mode: str) -> Path:
    (root / "guimp").mkdir(parents=True)
    (root / "guimp" / "__init__.py").write_text(textwrap.dedent(APP))
    (root / "pyproject.toml").write_text(textwrap.dedent(f"""
        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"
        [project]
        name = "guimp"
        version = "1.0"
        [project.gui-scripts]
        guimp = "guimp:main"
        [tool.pyxbuild]
        python = "3.12"
        mode = "{mode}"
    """))
    return root


def _win(path: Path) -> str:
    return str(path) if ON_WINDOWS else "Z:" + str(path).replace("/", "\\")


def _host(win_path: str) -> Path:
    if ON_WINDOWS:
        return Path(win_path)
    if win_path[:2].upper() == "Z:":
        return Path(win_path[2:].replace("\\", "/"))
    prefix = Path(os.environ.get("WINEPREFIX", Path.home() / ".cache" / "pyxbuild-wine"))
    return prefix / ("drive_" + win_path[0].lower()) / win_path[3:].replace("\\", "/")


# Starts a program the way Explorer does: no standard handles at all, so a GUI
# process really gets sys.stdout = None (a test runner would hand it pipes).
NO_HANDLES = """
import subprocess, sys
si = subprocess.STARTUPINFO()
si.dwFlags = subprocess.STARTF_USESTDHANDLES
si.hStdInput = si.hStdOutput = si.hStdError = 0
sys.exit(subprocess.call(sys.argv[1:], startupinfo=si, close_fds=True))
"""


def _build(proj: Path, cache: Path) -> None:
    old = os.environ.get("PYXBUILD_CACHE_DIR")
    os.environ["PYXBUILD_CACHE_DIR"] = str(cache)
    try:
        assert cli.main(["build", str(proj), "-q"]) == 0
    finally:
        if old is None:
            os.environ.pop("PYXBUILD_CACHE_DIR", None)
        else:
            os.environ["PYXBUILD_CACHE_DIR"] = old


@pytest.fixture(scope="module")
def builds(tmp_path_factory, shared_cache):
    root = tmp_path_factory.mktemp("guimp")
    for mode in ("folder", "onefile"):
        _build(_project(root / mode, mode), shared_cache)
    folder = root / "folder" / "dist" / "guimp-win_amd64"
    helper = root / "no_handles.py"
    helper.write_text(NO_HANDLES)
    return {"folder": folder / "guimp.exe", "onefile": root / "onefile" / "dist" / "guimp.exe",
            "python": folder / "runtime" / "python.exe", "helper": helper}


@pytest.mark.parametrize("mode", ["folder", "onefile"])
def test_gui_app_with_tkinter_and_multiprocessing(mode, builds, tmp_path):
    out = tmp_path / "result.json"
    env = {"PYX_ONEFILE_DIR": _win(tmp_path / "pyx"), "PYXBUILD_BOOT_NO_DIALOG": "1"}
    proc = run_exe(builds["python"], "-I", _win(builds["helper"]), _win(builds[mode]),
                   _win(out), env=env, timeout=900)
    assert proc.returncode == 0, proc.stderr
    results = json.loads(out.read_text(encoding="utf-8"))
    assert "error" not in results, results
    assert results["parent_executable"] == "pythonw.exe"
    assert results["pool"] == 285 and results["executor"] == 30
    assert results["worker_exception"] == "worker failed on 7"
    # writes to sys.stdout/sys.stderr in pythonw workers must not fail ...
    assert "chatty_error" not in results, results["chatty_error"]
    assert results["chatty_pids"] >= 1
    # ... and end up in the app's log next to the parent's output
    assert results["log"] and results["log"].endswith("guimp.log"), results["log"]
    log = _host(results["log"]).read_text(encoding="utf-8")
    assert "stdout from worker 3" in log and "stderr from worker 0" in log
    if ON_WINDOWS:
        assert results["tk"] == "window", results["tk"]
