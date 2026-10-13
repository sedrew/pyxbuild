"""include, strip-sources, bytecode-optimize, slim site presets, build-info,
PNG icons, XDG data folder and GUI log templates."""

from __future__ import annotations

import importlib.util
import json
import os
import struct
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest

from conftest import ROOT
from pyxbuild.builder import (
    FOREIGN_BINARIES,
    compile_bytecode,
    copy_includes,
    install_excludes,
    strip_sources,
    windows_only_settings,
)
from pyxbuild.config import load_config
from pyxbuild.errors import ConfigError, PyxbuildError
from pyxbuild.resources import RT_GROUP_ICON, RT_ICON, launcher_resources, read_icon
from pyxbuild.targets import LINUX_X86_64, WIN_AMD64
from test_resources import png

HOST_PY = f"{sys.version_info.major}.{sys.version_info.minor}"


def project(tmp_path: Path, tool: str = "") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text(textwrap.dedent("""
        [project]
        name = "MyApp"
        version = "1.0"
        requires-python = ">=3.11"
        [project.scripts]
        myapp = "myapp:main"
        [tool.pyxbuild]
    """) + textwrap.dedent(tool))
    return tmp_path


# --- configuration -------------------------------------------------------------

def test_new_settings_parse(tmp_path):
    cfg = load_config(project(tmp_path, """
        include = ["assets/**", "LICENSE"]
        strip-sources = true
        bytecode-optimize = 2
        gui-log = "{data_dir}/logs/%Y-%m-%d.log"
        icon = ["icons/16.png", "icons/256.png"]
        slim = ["tests", "panda3d-tools"]
        [tool.pyxbuild.build-info]
        channel = "beta"
        commit = "abc123"
    """))
    assert cfg.include == ["assets/**", "LICENSE"]
    assert cfg.strip_sources and cfg.bytecode  # strip-sources needs the .pyc files
    assert cfg.bytecode_optimize == 2
    assert cfg.gui_log == "{data_dir}/logs/%Y-%m-%d.log"
    assert cfg.icon == [tmp_path / "icons/16.png", tmp_path / "icons/256.png"]
    assert cfg.build_info == {"channel": "beta", "commit": "abc123"}
    assert "panda3d_tools/" in install_excludes(cfg)
    assert set(FOREIGN_BINARIES) <= set(install_excludes(cfg))


@pytest.mark.parametrize("tool, message", [
    ("bytecode-optimize = 3", "bytecode-optimize"),
    ("strip-sources = 1", "strip-sources"),
    ('slim = ["nope"]', "nope"),
    ("[tool.pyxbuild.build-info]\nn = 1", "build-info"),
    ('include = "assets"', "include"),
])
def test_bad_settings(tmp_path, tool, message):
    with pytest.raises(ConfigError, match=message):
        load_config(project(tmp_path, tool))


def test_windows_only_settings_are_listed(tmp_path):
    assert windows_only_settings(load_config(project(tmp_path))) == []
    cfg = load_config(project(tmp_path, """
        icon = "app.ico"
        app-id = "Acme.MyApp"
        onefile-progress = "classic"
        [tool.pyxbuild.version-info]
        company-name = "ACME"
    """))
    assert windows_only_settings(cfg) == ["icon", "version-info", "app-id", "onefile-progress"]


# --- include -------------------------------------------------------------------

def test_include_copies_matching_project_files(tmp_path):
    root = project(tmp_path / "p", 'include = ["assets/**", "LICENSE"]')
    (root / "assets" / "img").mkdir(parents=True)
    (root / "assets" / "img" / "a.png").write_bytes(b"png")
    (root / "assets" / "music.ogg").write_bytes(b"ogg")
    (root / "LICENSE").write_text("MIT")
    (root / "notes.txt").write_text("not included")
    (root / ".venv" / "assets").mkdir(parents=True)
    (root / ".venv" / "assets" / "x").write_text("skipped")
    (root / "dist" / "assets").mkdir(parents=True)
    (root / "dist" / "assets" / "old").write_text("skipped: the output folder")
    cfg = load_config(root)
    app = tmp_path / "app"
    assert copy_includes(cfg, app, WIN_AMD64) == 3
    files = sorted(p.relative_to(app).as_posix() for p in app.rglob("*") if p.is_file())
    assert files == ["LICENSE", "assets/img/a.png", "assets/music.ogg"]


def test_include_errors(tmp_path):
    root = project(tmp_path / "p", 'include = ["data/**"]')
    with pytest.raises(PyxbuildError, match="matched no files"):
        copy_includes(load_config(root), tmp_path / "a1", WIN_AMD64)
    (root / "data").mkdir()
    (root / "data" / "aux.txt").write_text("reserved name on Windows")
    with pytest.raises(PyxbuildError, match="aux.txt"):
        copy_includes(load_config(root), tmp_path / "a2", WIN_AMD64)
    assert copy_includes(load_config(root), tmp_path / "a3", LINUX_X86_64) == 1
    root = project(tmp_path / "q", 'include = ["runtime/**"]')
    (root / "runtime").mkdir()
    (root / "runtime" / "x.py").write_text("")
    with pytest.raises(PyxbuildError, match="overwrite"):
        copy_includes(load_config(root), tmp_path / "a4", WIN_AMD64)


# --- strip-sources and bytecode-optimize ------------------------------------------

def _site(tmp_path: Path) -> tuple[Path, Path]:
    """A tiny 'runtime' (stdlib folder + site-packages) and the project's wheel."""
    runtime = tmp_path / "runtime"
    stdlib = runtime / "Lib"
    stdlib.mkdir(parents=True)
    (stdlib / "helper.py").write_text("X = 1\n")
    site = stdlib / "site-packages"
    (site / "myapp").mkdir(parents=True)
    source = 'def main():\n    """Doc."""\n    assert False, "asserts kept"\n    return __doc__\n'
    (site / "myapp" / "__init__.py").write_text('"""Module doc."""\n' + source)
    (site / "other.py").write_text("Y = 2\n")  # a dependency: keeps its source
    wheel = tmp_path / "myapp-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr("myapp/__init__.py", "")
        zf.writestr("myapp-1.0.dist-info/METADATA", "")
    return runtime, wheel


def _import_main(site: Path, name: str = "myapp"):
    code = (f"import sys; sys.path.insert(0, {str(site)!r}); import {name} as m; "
            "print(m.__file__.rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1]); "
            "print(repr(m.__doc__))\n"
            "try:\n    m.main(); print('assert skipped')\n"
            "except AssertionError: print('assert ran')")
    return subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                          text=True, check=True).stdout.split("\n")


@pytest.mark.parametrize("optimize, doc, asserts", [
    (0, "'Module doc.'", "assert ran"),
    (2, "None", "assert skipped"),
])
def test_strip_sources_ships_sourceless_pyc(tmp_path, optimize, doc, asserts):
    runtime, wheel = _site(tmp_path)
    compile_bytecode(runtime, HOST_PY, stdlib="Lib", optimize=optimize)
    site = runtime / "Lib" / "site-packages"
    assert strip_sources(site, wheel, HOST_PY) == 1
    assert not (site / "myapp" / "__init__.py").exists()
    assert (site / "myapp" / "__init__.pyc").is_file()
    assert (site / "other.py").is_file()  # only the project's own modules
    name, docstring, assertion = _import_main(site)[:3]
    assert (name, docstring, assertion) == ("__init__.pyc", doc, asserts)


def test_optimized_bytecode_keeps_the_plain_cache_name(tmp_path):
    """-O/-OO .pyc are stored under the name a normal interpreter loads."""
    runtime, _ = _site(tmp_path)
    compile_bytecode(runtime, HOST_PY, stdlib="Lib", optimize=1)
    cache = runtime / "Lib" / "site-packages" / "myapp" / "__pycache__"
    assert [p.name for p in cache.iterdir()] == [
        f"__init__.cpython-{HOST_PY.replace('.', '')}.pyc"]


# --- PNG icons -----------------------------------------------------------------

def test_icon_from_pngs(tmp_path):
    paths = []
    for size in (256, 16, 48):
        paths.append(tmp_path / f"{size}.png")
        paths[-1].write_bytes(png(size, size))
    images = read_icon(paths)
    assert [(i.width, i.height) for i in images] == [(16, 16), (48, 48), (0, 0)]
    assert all(i.data.startswith(b"\x89PNG") for i in images)
    res = launcher_resources(script="a", project_name="A", version="1.0", description=None,
                             version_info_cfg={}, icon=paths)
    assert len(res[RT_ICON]) == 3
    count = struct.unpack("<HHH", res[RT_GROUP_ICON][1][:6])[2]
    assert count == 3


def test_bad_png_icons(tmp_path):
    big = tmp_path / "big.png"
    big.write_bytes(png(300, 300))
    with pytest.raises(ConfigError, match="at most 256x256"):
        read_icon(big)
    jpg = tmp_path / "x.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xe0" + b"\0" * 40)
    with pytest.raises(ConfigError, match="neither an .ico nor a PNG"):
        read_icon(jpg)


# --- boot script ---------------------------------------------------------------

@pytest.fixture
def boot():
    path = ROOT / "src" / "pyxbuild" / "_boot" / "_pyxbuild_boot.py"
    spec = importlib.util.spec_from_file_location("_pyxbuild_boot_features", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(os.name == "nt", reason="Windows keeps the home-folder fallback")
def test_data_home_follows_xdg(boot, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert boot._user_data_home() == str(tmp_path / "xdg")
    monkeypatch.setenv("XDG_DATA_HOME", "relative/is/ignored")
    assert boot._user_data_home() == os.path.expanduser("~/.local/share")
    monkeypatch.delenv("XDG_DATA_HOME")
    assert boot._user_data_home() == os.path.expanduser("~/.local/share")


def test_gui_log_template(boot, tmp_path, monkeypatch):
    (tmp_path / "logpkg").mkdir()
    (tmp_path / "logpkg" / "__init__.py").write_text("def run():\n    print('hello log')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    (tmp_path / "pyxbuild.json").write_text(json.dumps({
        "project": {"name": "MyApp"},
        "launchers": {"gui": {"entry-point": "logpkg:run", "gui": True}},
        "boot": {"gui-log": "{data_dir}/journal/%Y.log"}}))
    monkeypatch.setattr(sys, "argv", ["-c", "gui.exe"])
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    with pytest.raises(SystemExit):
        boot.main("gui")
    sys.stdout.flush()
    logs = list((tmp_path / "local" / "MyApp" / "journal").glob("*.log"))
    assert [p.stem.isdigit() and len(p.stem) == 4 for p in logs] == [True]
    assert "hello log" in logs[0].read_text()


# --- end to end (Linux host, network) -----------------------------------------

from test_linux import NATIVE, needs_linux  # noqa: E402


@needs_linux
@pytest.mark.slow
@pytest.mark.network
def test_features_end_to_end(tmp_path, shared_cache, monkeypatch, capsys):
    from pyxbuild import cli

    monkeypatch.setenv("PYXBUILD_CACHE_DIR", str(shared_cache))
    root = tmp_path / "p"
    (root / "src" / "game").mkdir(parents=True)
    (root / "src" / "game" / "__init__.py").write_text(textwrap.dedent('''
        """Game."""
        import json, os, sys
        def main():
            assets = os.environ["GAME_ASSETS"]
            print("ASSET", open(os.path.join(assets, "level.txt")).read())
            print("FILE", __file__.rsplit("/", 1)[-1], "DOC", __doc__)
            with open(os.path.join(sys.prefix, "pyxbuild.json")) as f:
                print("INFO", json.load(f)["build-info"])
    '''))
    (root / "assets").mkdir()
    (root / "assets" / "level.txt").write_text("level-1")
    (root / "pyproject.toml").write_text(textwrap.dedent(f"""
        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"
        [project]
        name = "game"
        version = "1.0"
        requires-python = ">=3.11"
        [project.scripts]
        game = "game:main"
        [tool.pyxbuild]
        python = "3.12"
        targets = ["{NATIVE}"]
        include = ["assets/**"]
        strip-sources = true
        bytecode-optimize = 2
        icon = "missing.ico"
        [tool.pyxbuild.env]
        GAME_ASSETS = "{{app_dir}}/assets"
        [tool.pyxbuild.build-info]
        channel = "jam"
    """))
    assert cli.main(["build", str(root)]) == 0
    assert "Windows-only, ignored for this target: icon" in capsys.readouterr().err
    app = root / "dist" / f"game-{NATIVE}"
    r = subprocess.run([str(app / "game")], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    assert "ASSET level-1" in r.stdout
    assert "FILE __init__.pyc DOC None" in r.stdout
    assert "INFO {'channel': 'jam'}" in r.stdout
