"""End-to-end: build real projects from PyPI wheels and run the result.

On Linux/macOS the executables run under Wine (set $PYXBUILD_WINE to Wine 10+,
see tools/get_wine.sh); on Windows they run natively. Wine is a convenience;
the authoritative run is the windows-latest CI job.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from conftest import GOLDEN, file_digest_tree, run_exe, wine_available
from pyxbuild import cli
from pyxbuild.launcher import decode_manifest

pytestmark = [pytest.mark.slow, pytest.mark.network]

needs_runner = pytest.mark.skipif(not wine_available(), reason="needs Windows or Wine")
ON_WINDOWS = sys.platform == "win32"


@contextmanager
def env_vars(**values: str):
    old = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def copy_project(name: str, dest: Path) -> Path:
    proj = dest / name
    shutil.copytree(GOLDEN / name, proj,
                    ignore=shutil.ignore_patterns("dist", "pylock.*.toml", "__pycache__"))
    return proj


def build(proj: Path, cache: Path, *args: str) -> Path:
    with env_vars(PYXBUILD_CACHE_DIR=str(cache)):
        rc = cli.main(["build", str(proj), "-q", *args])
    assert rc == 0, f"pyxbuild build failed for {proj}"
    apps = [d for d in (proj / "dist").iterdir() if d.is_dir() and d.name.endswith("-win_amd64")]
    assert len(apps) == 1, apps
    return apps[0]


@pytest.fixture(scope="module")
def hello_app(tmp_path_factory, shared_cache):
    return build(copy_project("hello", tmp_path_factory.mktemp("hello")), shared_cache,
                 "--python", "3.12")


@pytest.fixture(scope="module")
def features_app(tmp_path_factory, shared_cache):
    return build(copy_project("features", tmp_path_factory.mktemp("features")), shared_cache,
                 "--python", "3.12")


def test_folder_layout(hello_app):
    assert (hello_app / "hello.exe").is_file()
    rt = hello_app / "runtime"
    assert (rt / "python.exe").is_file() and (rt / "pythonw.exe").is_file()
    assert (rt / "python312.dll").is_file()
    assert (rt / "vcruntime140.dll").is_file()
    assert not (rt / "Lib" / "site-packages" / "pip").exists()
    assert not (rt / "include").exists() and not (rt / "libs").exists()
    site = rt / "Lib" / "site-packages"
    assert (site / "hello-1.0.0.dist-info" / "INSTALLER").read_text() == "pyxbuild"
    assert any(site.glob("requests-*.dist-info"))
    assert decode_manifest((hello_app / "hello.exe").read_bytes())["python"] == (
        "runtime\\python.exe")
    assert not list(hello_app.rglob("__pycache__"))
    # files pyxbuild writes must be byte-identical on every host (no CRLF on Windows)
    assert b"\r" not in (rt / "pyxbuild.json").read_bytes()
    assert b"\r" not in (hello_app.parent.parent / "pylock.win_amd64.toml").read_bytes()


@needs_runner
def test_hello_arguments_and_exit_code(hello_app):
    proc = run_exe(hello_app / "hello.exe", "a b", 'c"d', "привет", "--exit=5")
    assert proc.returncode == 5, proc.stderr
    assert "argv " + ascii(["a b", 'c"d', "привет", "--exit=5"]) in proc.stdout
    assert f"executable {_win(hello_app / 'runtime' / 'python.exe')}" in proc.stdout


@needs_runner
@pytest.mark.skipif(bool(os.environ.get("PYXBUILD_TEST_OFFLINE")), reason="offline")
def test_hello_https_with_certifi(hello_app):
    proc = run_exe(hello_app / "hello.exe", "--https")
    assert proc.returncode == 0, proc.stderr
    assert "https 200" in proc.stdout


@needs_runner
def test_dependency_console_script(hello_app):
    proc = run_exe(hello_app / "runtime" / "Scripts" / "normalizer.exe", "--version")
    assert proc.returncode == 0, proc.stderr
    assert "Charset-Normalizer" in proc.stdout


@needs_runner
def test_app_folder_can_be_moved(hello_app, tmp_path):
    moved = tmp_path / "Program Files" / "My Äpp"
    shutil.copytree(hello_app, moved)
    proc = run_exe(moved / "hello.exe", "x")
    assert proc.returncode == 0, proc.stderr
    assert "argv ['x']" in proc.stdout


def test_features_launchers_have_icon_and_version(features_app):
    pefile = pytest.importorskip("pefile")
    for name in ("features.exe", "features-gui.exe"):
        p = pefile.PE(str(features_app / name))
        strings = {k.decode(): v.decode() for fi in p.FileInfo[0] if fi.Key == b"StringFileInfo"
                   for st in fi.StringTable for k, v in st.entries.items()}
        assert strings["CompanyName"] == "pyxbuild tests"
        assert strings["FileVersion"] == "0.3.0"
        assert strings["OriginalFilename"] == name
        types = {e.id for e in p.DIRECTORY_ENTRY_RESOURCE.entries}
        assert {3, 14, 16, 24} <= types


@needs_runner
def test_features_console(features_app):
    # a Tcl from another installation must not break the bundled one
    proc = run_exe(features_app / "features.exe", timeout=900,
                   env={"TCL_LIBRARY": "C:\\Python27\\tcl\\tcl8.5"})
    assert "FAIL" not in proc.stdout, proc.stdout + proc.stderr
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for name in ("argv", "boot_env", "utf8_mode", "isolation", "metadata", "resources",
                 "stdlib_extensions", "tcl", "subprocess_python", "multiprocessing"):
        assert f"OK {name}" in proc.stdout


@needs_runner
def test_features_gui_subsystem(features_app, tmp_path):
    proc = run_exe(features_app / "features-gui.exe", "result.txt", cwd=tmp_path, timeout=900)
    text = (tmp_path / "result.txt").read_text(encoding="utf-8")
    assert proc.returncode == 0, text
    # stdout is a pipe here because the test redirects it; started from Explorer
    # a GUI app has no standard handles and sys.stdout is None.
    assert "stdout is None:" in text
    assert "OK multiprocessing" in text
    assert "FAIL" not in text, text


@needs_runner
def test_utf8_mode_output(features_app):
    proc = run_exe(features_app / "features.exe", "--echo", "привет", "日本")
    assert proc.returncode == 0, proc.stderr
    assert "ARGS ['привет', '日本']" in proc.stdout


@needs_runner
def test_exit_codes_and_exceptions(features_app):
    assert run_exe(features_app / "features.exe", "--exit", "7").returncode == 7
    assert run_exe(features_app / "features.exe", "--exit", "0").returncode == 0
    proc = run_exe(features_app / "features.exe", "--raise")
    assert proc.returncode == 1
    assert "RuntimeError: boom" in proc.stderr


@pytest.mark.skipif(not ON_WINDOWS, reason="console control events need real Windows")
def test_ctrl_break_reaches_the_app_and_exit_code_is_forwarded(features_app):
    proc = subprocess.Popen([str(features_app / "features.exe"), "--wait-break"],
                            stdout=subprocess.PIPE, text=True,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    assert proc.stdout.readline().strip() == "READY"
    os.kill(proc.pid, __import__("signal").CTRL_BREAK_EVENT)
    out, _ = proc.communicate(timeout=60)
    assert "GOT SIGBREAK" in out
    assert proc.returncode == 42


def test_reproducible_build(tmp_path, shared_cache, hello_app):
    """Two builds from different directories give byte-identical results."""
    first = build(copy_project("hello", tmp_path / "one"), shared_cache, "--python", "3.12",
                  "--zip")
    second = build(copy_project("hello", tmp_path / "two" / "nested"), shared_cache,
                   "--python", "3.12", "--zip")
    assert file_digest_tree(first) == file_digest_tree(second)
    # hello_app has been run, so Python has written __pycache__ into it
    ran = {k: v for k, v in file_digest_tree(hello_app).items() if "__pycache__" not in k}
    assert file_digest_tree(first) == ran
    zips = [next(p.parent.glob("*.zip")).read_bytes() for p in (first, second)]
    assert zips[0] == zips[1]


def test_frozen_lock_and_offline_rebuild(tmp_path, shared_cache):
    proj = copy_project("hello", tmp_path)
    build(proj, shared_cache, "--python", "3.12")
    lock = proj / "pylock.win_amd64.toml"
    before = lock.read_text()
    with env_vars(PYXBUILD_CACHE_DIR=str(shared_cache)):
        assert cli.main(["build", str(proj), "-q", "--python", "3.12", "--offline"]) == 0
        # a stale lock is an error with --frozen
        assert cli.main(["build", str(proj), "-q", "--python", "3.13", "--frozen"]) == 1
    assert lock.read_text() == before


@needs_runner
@pytest.mark.skipif(not shutil.which("osslsigncode") or not shutil.which("openssl"),
                    reason="needs osslsigncode and openssl")
def test_signed_launcher_still_runs(hello_app, tmp_path):
    from test_launcher_pe import make_test_certificate

    app = tmp_path / "signed"
    shutil.copytree(hello_app, app)
    pfx = make_test_certificate(tmp_path)
    with env_vars(PFX_PASSWORD="test"):
        assert cli.main(["sign", str(app), "--cert", str(pfx), "--password-env",
                         "PFX_PASSWORD"]) == 0
    proc = run_exe(app / "hello.exe", "signed", "--exit=3")
    assert proc.returncode == 3, proc.stderr
    assert "argv ['signed', '--exit=3']" in proc.stdout


def test_doctor_command(tmp_path, shared_cache, capsys):
    proj = copy_project("hello", tmp_path)
    with env_vars(PYXBUILD_CACHE_DIR=str(shared_cache)):
        rc = cli.main(["doctor", str(proj), "-q", "--python", "3.12"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "== win_amd64 ==" in out
    assert not list(proj.glob("pylock*.toml")), "doctor must not write the lock file"


def _win(path: Path) -> str:
    return str(path) if ON_WINDOWS else "Z:" + str(path).replace("/", "\\")


# --- golden set -------------------------------------------------------------------

def _pythons(var: str, default: str) -> list[str]:
    return os.environ.get(var, default).split(",")


GOLDEN_MATRIX = [("level1", py) for py in _pythons("PYXBUILD_GOLDEN1_PYTHONS",
                                                   "3.11,3.12,3.13,3.14")]
GOLDEN_MATRIX += [("level2", py) for py in _pythons("PYXBUILD_GOLDEN2_PYTHONS", "3.12,3.13")]


@pytest.mark.golden
@pytest.mark.parametrize(("app", "python"), GOLDEN_MATRIX)
def test_golden(app, python, tmp_path, shared_cache):
    app_dir = build(copy_project(app, tmp_path), shared_cache, "--python", python)
    if not wine_available():
        pytest.skip("built; no Windows/Wine to run it")
    env = {}
    if not ON_WINDOWS:
        env["PYXBUILD_TEST_NO_DISPLAY"] = "1"  # Tk()/Qt windows need a display
        env["PYXBUILD_TEST_QPA"] = "offscreen"
    proc = run_exe(app_dir / f"{app}.exe", env=env, timeout=1800)
    assert "FAIL" not in proc.stdout, proc.stdout + proc.stderr
    assert proc.returncode == 0, proc.stdout + proc.stderr


@needs_runner
@pytest.mark.skipif(ON_WINDOWS, reason="read-only via POSIX permissions")
def test_bytecode_app_runs_from_read_only_folder(tmp_path, shared_cache):
    proj = copy_project("hello", tmp_path)
    text = (proj / "pyproject.toml").read_text() + "\n[tool.pyxbuild]\nbytecode = true\n"
    (proj / "pyproject.toml").write_text(text)
    app = build(proj, shared_cache, "--python", "3.12")
    before = file_digest_tree(app)
    assert any(k.endswith(".cpython-312.pyc") and "/requests/" in k for k in before)
    for p in [app, *app.rglob("*")]:
        p.chmod(p.stat().st_mode & ~0o222)
    try:
        proc = run_exe(app / "hello.exe", "--exit=0")
        assert proc.returncode == 0, proc.stderr
        assert file_digest_tree(app) == before  # nothing written next to the app
    finally:
        for p in [app, *app.rglob("*")]:
            p.chmod(p.stat().st_mode | 0o200)


def test_external_pep751_lock_is_used_and_never_overwritten(tmp_path, shared_cache):
    proj = copy_project("hello", tmp_path)
    build(proj, shared_cache, "--python", "3.12")
    ours = (proj / "pylock.win_amd64.toml").read_text()
    foreign = ours.split("\n[tool.pyxbuild]")[0].replace("created-by = \"uv\"",
                                                          "created-by = \"pdm\"") + "\n"
    (proj / "pylock.win_amd64.toml").write_text(foreign)
    build(proj, shared_cache, "--python", "3.12")
    assert (proj / "pylock.win_amd64.toml").read_text() == foreign
    with env_vars(PYXBUILD_CACHE_DIR=str(shared_cache)):
        assert cli.main(["build", str(proj), "-q", "--python", "3.12", "--relock"]) == 1
    assert (proj / "pylock.win_amd64.toml").read_text() == foreign
