"""Onefile executables: the archive format, the launcher's extraction cache and
temp mode, run on Windows or under Wine."""

import os
import random
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from conftest import run_exe, wine_available, wine_command
from pyxbuild import cli
from pyxbuild.config import load_config
from pyxbuild.errors import ConfigError
from pyxbuild.launcher import decode_manifest, write_onefile
from pyxbuild.onefile import CHUNK, ENTRY, HEADER, MAGIC, read_archive, write_archive

ON_WINDOWS = sys.platform == "win32"

# --- archive format --------------------------------------------------------


def _tree(root: Path) -> dict[str, bytes]:
    rng = random.Random(3)
    files = {
        "runtime/python.exe": b"MZ" + rng.randbytes(5000),
        "runtime/Lib/site-packages/pkg/__init__.py": b"x = 1\n" * 1000,
        "runtime/Lib/site-packages/pkg/empty.txt": b"",
        "runtime/Lib/site-packages/données/π.txt": "привет".encode(),
        "runtime/big.bin": rng.randbytes(int(CHUNK * 2.5)),  # spans chunks
        "runtime/zeros.bin": bytes(CHUNK * 3),
        "app.exe": b"launcher",
    }
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return files


def test_archive_roundtrip_and_skip(tmp_path):
    files = _tree(tmp_path / "app")
    info = write_archive(tmp_path / "app", tmp_path / "a.pyxarc", skip={"app.exe"})
    del files["app.exe"]
    assert read_archive((tmp_path / "a.pyxarc").read_bytes()) == files
    assert info.files == len(files)
    assert info.size < info.stream_size  # compressed


def test_archive_is_deterministic_and_stores_incompressible_chunks(tmp_path):
    _tree(tmp_path / "app")
    a = write_archive(tmp_path / "app", tmp_path / "a.pyxarc")
    b = write_archive(tmp_path / "app", tmp_path / "b.pyxarc")
    assert a.sha256 == b.sha256
    data = (tmp_path / "a.pyxarc").read_bytes()
    magic, _, count, *_ = HEADER.unpack_from(data)
    assert magic == MAGIC
    entries = [ENTRY.unpack_from(data, HEADER.size + i * ENTRY.size) for i in range(count)]
    assert any(c == u for _, c, u in entries)       # random data: stored as is
    assert any(c < u // 100 for _, c, u in entries)  # zeros: deflated


def test_onefile_manifest_points_at_the_archive(tmp_path):
    from pyxbuild.config import EntryPoint

    _tree(tmp_path / "app")
    info = write_archive(tmp_path / "app", tmp_path / "a.pyxarc")
    exe = tmp_path / "app1.exe"
    write_onefile(exe, EntryPoint("app1", "app", "main", gui=False), "win_amd64", info,
                  app="demo", extract="temp")
    data = exe.read_bytes()
    m = decode_manifest(data)
    off, size = int(m["archive-offset"]), int(m["archive-size"])
    assert data[off:off + 8] == MAGIC and size == info.size
    assert m["onefile"] == info.sha256 and m["extract"] == "temp" and m["app"] == "demo"
    assert m["python"] == "runtime\\python.exe"
    assert read_archive(data[off:off + size])["runtime/python.exe"].startswith(b"MZ")


def test_onefile_extract_option_is_validated(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "a"\nversion = "1"\n[project.scripts]\na = "a:main"\n'
        '[tool.pyxbuild]\nmode = "onefile"\nonefile-extract = "ram"\n')
    with pytest.raises(ConfigError, match="onefile-extract"):
        load_config(tmp_path)


# --- end to end ------------------------------------------------------------

APP = '''
import os, sys

def work(x):
    return x * x

def main():
    args = sys.argv[1:]
    print("EXE", sys.executable)
    print("ARGV", args)
    print("APPDIR", os.environ.get("APPDIR"))
    print("EXEDIR", os.environ.get("EXEDIR"))
    print("TEMP", os.environ.get("TEMP"))
    if args[:1] == ["mp"]:
        import multiprocessing
        with multiprocessing.Pool(2) as pool:
            print("MP", sum(pool.map(work, range(10))))
    if args[:1] == ["sleep"]:
        import time
        time.sleep(float(args[1]))
    if args[:1] == ["exit"]:
        return int(args[1])
    return 0
'''


def _project(root: Path, extract: str) -> Path:
    (root / "oneapp").mkdir(parents=True)
    (root / "oneapp" / "__init__.py").write_text(textwrap.dedent(APP))
    (root / "pyproject.toml").write_text(textwrap.dedent(f"""
        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"
        [project]
        name = "oneapp"
        version = "1.0"
        description = "Однофайловое приложение"
        [project.scripts]
        oneapp = "oneapp:main"
        [tool.pyxbuild]
        python = "3.12"
        mode = "onefile"
        onefile-extract = "{extract}"
        [tool.pyxbuild.env]
        APPDIR = "{{app_dir}}"
        EXEDIR = "{{exe_dir}}"
    """))
    return root


def _win(path: Path) -> str:
    return str(path) if ON_WINDOWS else "Z:" + str(path).replace("/", "\\")


def _host(win_path: str) -> Path:
    """A path printed by the app, as seen from the test (Wine: drive mapping)."""
    if ON_WINDOWS:
        return Path(win_path)
    if win_path[:2].upper() == "Z:":
        return Path(win_path[2:].replace("\\", "/"))
    prefix = Path(os.environ.get("WINEPREFIX", Path.home() / ".cache" / "pyxbuild-wine"))
    return prefix / ("drive_" + win_path[0].lower()) / win_path[3:].replace("\\", "/")


def _lines(out: str) -> dict[str, str]:
    return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)


needs_runner = pytest.mark.skipif(not wine_available(), reason="needs Windows or Wine")


def _build(proj: Path, cache: Path) -> Path:
    old = os.environ.get("PYXBUILD_CACHE_DIR")
    os.environ["PYXBUILD_CACHE_DIR"] = str(cache)
    try:
        assert cli.main(["build", str(proj), "-q"]) == 0
    finally:
        if old is None:
            os.environ.pop("PYXBUILD_CACHE_DIR", None)
        else:
            os.environ["PYXBUILD_CACHE_DIR"] = old
    return proj / "dist" / "oneapp.exe"


@pytest.fixture(scope="module")
def cache_exe(tmp_path_factory, shared_cache):
    return _build(_project(tmp_path_factory.mktemp("onefile-cache"), "cache"), shared_cache)


@pytest.fixture(scope="module")
def temp_exe(tmp_path_factory, shared_cache):
    return _build(_project(tmp_path_factory.mktemp("onefile-temp"), "temp"), shared_cache)


def _hash12(exe: Path) -> str:
    return decode_manifest(exe.read_bytes())["onefile"][:12]


@pytest.mark.slow
@pytest.mark.network
@needs_runner
def test_cache_extracts_once_and_reuses(cache_exe, tmp_path):
    root = tmp_path / "pyx"
    env = {"PYX_ONEFILE_DIR": _win(root)}
    first = run_exe(cache_exe, "exit", "7", "a b", env=env)
    assert first.returncode == 7, first.stderr
    out = _lines(first.stdout)
    version = root / "oneapp" / _hash12(cache_exe)
    assert _host(out["EXE"]) == version / "runtime" / "python.exe"
    assert out["ARGV"] == "['exit', '7', 'a b']"
    assert _host(out["APPDIR"]) == version              # bundle data: the extracted copy
    assert _host(out["EXEDIR"]) == cache_exe.parent     # where the user's .exe is
    marker = version / ".pyx-complete"
    stamp = marker.stat().st_mtime_ns
    assert not [p for p in (root / "oneapp").iterdir() if p.name != version.name]

    second = run_exe(cache_exe, env=env)
    assert second.returncode == 0, second.stderr
    assert marker.stat().st_mtime_ns == stamp  # not extracted again

    reset = run_exe(cache_exe, env={**env, "PYX_ONEFILE_RESET": "1"})
    assert reset.returncode == 0, reset.stderr
    assert marker.stat().st_mtime_ns != stamp


@pytest.mark.slow
@pytest.mark.network
@needs_runner
def test_cache_repairs_and_cleans_up(cache_exe, tmp_path):
    root = tmp_path / "pyx"
    appdir = root / "oneapp"
    current = appdir / _hash12(cache_exe)
    old = time.time() - 3600
    # an interrupted extraction of this version, an abandoned temporary
    # folder, and two older versions
    (current / "runtime").mkdir(parents=True)
    (appdir / (current.name + ".tmp-dead")).mkdir()
    for name, age in (("aaaaaaaaaaaa", 7200), ("bbbbbbbbbbbb", 3600)):
        (appdir / name / "runtime").mkdir(parents=True)
        (appdir / name / ".pyx-complete").write_text("old\n")
        os.utime(appdir / name, (time.time() - age, time.time() - age))
    os.utime(appdir / (current.name + ".tmp-dead"), (old, old))
    proc = run_exe(cache_exe, env={"PYX_ONEFILE_DIR": _win(root)})
    assert proc.returncode == 0, proc.stderr
    assert (current / ".pyx-complete").is_file()
    assert (current / "runtime" / "python.exe").is_file()
    left = {p.name for p in appdir.iterdir()}
    assert left == {"bbbbbbbbbbbb", current.name}, left  # newest previous version kept


@pytest.mark.slow
@pytest.mark.network
@needs_runner
def test_concurrent_first_starts_extract_once(cache_exe, tmp_path):
    root = tmp_path / "pyx"
    env = {**os.environ, "PYX_ONEFILE_DIR": _win(root), "WINEDEBUG": "-all"}
    cmd = [str(cache_exe)] if ON_WINDOWS else [wine_command(), str(cache_exe)]
    procs = [subprocess.Popen(cmd + [str(i)], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
             for i in range(3)]
    for p in procs:
        out, err = p.communicate(timeout=600)
        assert p.returncode == 0, err.decode(errors="replace")
    assert sorted(p.name for p in (root / "oneapp").iterdir()) == [_hash12(cache_exe)]


@pytest.mark.slow
@pytest.mark.network
@needs_runner
def test_multiprocessing_in_onefile(cache_exe, tmp_path):
    proc = run_exe(cache_exe, "mp", env={"PYX_ONEFILE_DIR": _win(tmp_path / "pyx")})
    assert proc.returncode == 0, proc.stderr
    assert "MP 285" in proc.stdout


@pytest.mark.slow
@pytest.mark.network
@needs_runner
def test_temp_mode_removes_its_folder(temp_exe):
    first = run_exe(temp_exe, "sleep", "0")
    assert first.returncode == 0, first.stderr
    out = _lines(first.stdout)
    extracted = _host(out["EXE"]).parent.parent
    temp = _host(out["TEMP"])
    assert extracted.parent == temp and extracted.name.startswith("pyx-oneapp-")
    assert not extracted.exists()
    # a folder left behind by a killed run is swept on the next start
    stale = temp / "pyx-oneapp-dead-beef"
    (stale / "runtime").mkdir(parents=True)
    os.utime(stale, (time.time() - 3600, time.time() - 3600))
    again = run_exe(temp_exe)
    assert again.returncode == 0, again.stderr
    assert not stale.exists()


@pytest.mark.slow
@pytest.mark.network
@needs_runner
@pytest.mark.skipif(not shutil.which("osslsigncode") or not shutil.which("openssl"),
                    reason="needs osslsigncode and openssl")
def test_signed_onefile_runs(cache_exe, tmp_path):
    from test_launcher_pe import make_test_certificate

    exe = tmp_path / "signed.exe"
    shutil.copy(cache_exe, exe)
    pfx = make_test_certificate(tmp_path)
    os.environ["PFX_PASSWORD"] = "test"
    try:
        assert cli.main(["sign", str(exe), "--cert", str(pfx),
                         "--password-env", "PFX_PASSWORD"]) == 0
    finally:
        os.environ.pop("PFX_PASSWORD", None)
    assert decode_manifest(exe.read_bytes())["onefile"]  # manifest found behind the signature
    proc = run_exe(exe, "exit", "4", env={"PYX_ONEFILE_DIR": _win(tmp_path / "pyx")})
    assert proc.returncode == 4, proc.stderr
