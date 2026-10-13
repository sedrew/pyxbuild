"""Linux targets (linux_x86_64, linux_aarch64): layout, launchers, install rules,
and end-to-end builds that run on a Linux host of the same architecture."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import tarfile
import textwrap
import zipfile
from pathlib import Path

import pytest

from pyxbuild.builder import slim_patterns, write_tarball
from pyxbuild.config import EntryPoint
from pyxbuild.install import InstallState, install_wheel
from pyxbuild.launcher import build_launcher, decode_manifest, stub_bytes
from pyxbuild.targets import LINUX_AARCH64, LINUX_X86_64, WIN_AMD64, get_target

NATIVE = {"x86_64": "linux_x86_64", "aarch64": "linux_aarch64"}.get(platform.machine())
ON_LINUX = sys.platform == "linux" and NATIVE is not None


def test_layout_tags_and_markers():
    t = get_target("linux_x86_64")
    assert t.interpreter("3.12") == "bin/python3.12"
    assert t.site_packages("3.13") == "lib/python3.13/site-packages"
    assert t.scripts_dir == "bin" and t.launcher_name("app") == "app"
    tags = [str(x) for x in t.tags("3.12")]
    assert tags[0] == "cp312-cp312-manylinux_2_28_x86_64"
    assert "cp312-abi3-manylinux2014_x86_64" in tags and "py3-none-any" in tags
    assert not any("win" in x or x.endswith("linux_x86_64") and "many" not in x for x in tags)
    env = LINUX_AARCH64.marker_environment("3.12.10")
    assert (env["sys_platform"], env["platform_machine"], env["os_name"]) == (
        "linux", "aarch64", "posix")
    # Windows is untouched
    assert WIN_AMD64.interpreter("3.12") == "python.exe" and WIN_AMD64.stdlib_dir("3.12") == "Lib"


def test_slim_presets_follow_the_layout():
    assert "lib/python3.12/test/" in slim_patterns(LINUX_X86_64, "3.12", "tests")
    assert "lib/libtcl*" in slim_patterns(LINUX_X86_64, "3.12", "tk")
    assert "Lib/test/" in slim_patterns(WIN_AMD64, "3.12", "tests")


@pytest.mark.parametrize("target", ["linux_x86_64", "linux_aarch64"])
def test_linux_launchers_are_elf_with_a_manifest(target):
    for onefile in (False, True):
        assert stub_bytes(target, gui=False, onefile=onefile)[:4] == b"\x7fELF"
    entry = EntryPoint("app", "app.main", "run", gui=False)
    exe = build_launcher(entry, target, python_rel="runtime", boot=True,
                         interpreter="runtime/bin/python3.12")
    m = decode_manifest(exe)
    assert m["python"] == "runtime/bin/python3.12" and m["flags"] == "-I"


def _wheel(path: Path, files: dict[str, str], scripts: dict[str, str] | None = None) -> Path:
    name = path.name.split("-")[0]
    with zipfile.ZipFile(path, "w") as zf:
        for rel, text in files.items():
            zf.writestr(rel, text)
        info = f"{name}-1.0.dist-info"
        zf.writestr(f"{info}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
        zf.writestr(f"{info}/WHEEL",
                    "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        if scripts:
            zf.writestr(f"{info}/entry_points.txt", "[console_scripts]\n" + "".join(
                f"{k} = {v}\n" for k, v in scripts.items()))
        zf.writestr(f"{info}/RECORD", "")
    return path


def test_install_uses_posix_rules(tmp_path):
    runtime = tmp_path / "runtime"
    (runtime / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    state = InstallState(runtime, target=LINUX_X86_64, python="3.12")
    install_wheel(_wheel(tmp_path / "Crypto-1.0-py3-none-any.whl",
                         {"Crypto/__init__.py": "A = 1\n", "Crypto/aux.py": ""},
                         {"crypto-tool": "Crypto:main"}), runtime, state)
    site = runtime / "lib" / "python3.12" / "site-packages"
    second = _wheel(tmp_path / "crypto-1.0-py3-none-any.whl", {"crypto/__init__.py": "B = 1\n"})
    probe = tmp_path / "CaseProbe"
    probe.write_text("")
    if (tmp_path / "caseprobe").exists():  # macOS/Windows host: one folder for both
        from pyxbuild.errors import PyxbuildError

        with pytest.raises(PyxbuildError, match="differs only by case"):
            install_wheel(second, runtime, state)
    else:  # case-sensitive: two packages
        install_wheel(second, runtime, state)
        assert (site / "crypto" / "__init__.py").exists()
    # "aux" is an ordinary name on Linux
    assert (site / "Crypto" / "aux.py").exists()
    script = (runtime / "bin" / "crypto-tool").read_text()
    assert script.startswith("#!/bin/sh\n'''exec' ") and "/python3.12\"" in script
    assert "from Crypto import main" in script
    assert os.access(runtime / "bin" / "crypto-tool", os.X_OK) or sys.platform == "win32"


def test_tarball_is_deterministic_and_keeps_modes(tmp_path):
    app = tmp_path / "app"
    (app / "runtime" / "bin").mkdir(parents=True)
    (app / "run").write_bytes(b"\x7fELF...")
    (app / "runtime" / "bin" / "python3.12").write_bytes(b"\x7fELF")
    (app / "runtime" / "data.txt").write_text("x")
    if sys.platform != "win32":
        (app / "run").chmod(0o755)
        (app / "runtime" / "bin" / "python3.12").chmod(0o755)
    write_tarball(app, tmp_path / "a.tar.gz", 315532800)
    write_tarball(app, tmp_path / "b.tar.gz", 315532800)
    assert (tmp_path / "a.tar.gz").read_bytes() == (tmp_path / "b.tar.gz").read_bytes()
    with tarfile.open(tmp_path / "a.tar.gz") as tf:
        modes = {m.name: m.mode for m in tf.getmembers()}
    assert modes["app/runtime/data.txt"] == 0o644
    if sys.platform != "win32":
        assert modes["app/run"] == 0o755


# --- end to end (Linux host, network) -----------------------------------------

APP = '''
import os, sys, time

def main():
    print("VERSION", "{version}", "python", sys.version.split()[0])
    print("argv", sys.argv[1:])
    print("prefix", sys.prefix)
    if "--sleep" in sys.argv:
        time.sleep(float(sys.argv[-1]))
    return 0
'''


def _project(root: Path, version: str = "1.0", mode: str = "folder", extract: str = "cache"):
    (root / "src" / "demo_app").mkdir(parents=True, exist_ok=True)
    (root / "src" / "demo_app" / "__init__.py").write_text(APP.replace("{version}", version))
    (root / "pyproject.toml").write_text(textwrap.dedent(f"""
        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"
        [project]
        name = "demo-app"
        version = "{version}"
        requires-python = ">=3.11"
        [project.scripts]
        demo = "demo_app:main"
        [tool.pyxbuild]
        python = "3.12"
        mode = "{mode}"
        onefile-extract = "{extract}"
        targets = ["{NATIVE or 'linux_x86_64'}"]
    """))
    return root


def _build(root: Path, *args: str) -> None:
    from pyxbuild import cli

    assert cli.main(["build", str(root), "-q", *args]) == 0


def _run(exe: Path, *args: str, env: dict | None = None, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(exe), *args], capture_output=True, text=True, timeout=300,
                          env={**os.environ, **(env or {})}, **kw)


needs_linux = pytest.mark.skipif(not ON_LINUX, reason="runs Linux executables natively")


@needs_linux
@pytest.mark.slow
@pytest.mark.network
def test_folder_build_runs_and_is_relocatable(tmp_path, shared_cache, monkeypatch):
    monkeypatch.setenv("PYXBUILD_CACHE_DIR", str(shared_cache))
    root = _project(tmp_path / "p")
    _build(root, "--zip")
    app = root / "dist" / f"demo-app-{NATIVE}"
    r = _run(app / "demo", "x", "ü")
    assert r.returncode == 0, r.stderr
    assert "VERSION 1.0 python 3.12" in r.stdout and "argv ['x', 'ü']" in r.stdout
    moved = tmp_path / "elsewhere" / "app"
    moved.parent.mkdir()
    app.rename(moved)
    assert "VERSION 1.0" in _run(moved / "demo").stdout
    # the dependency-style script in runtime/bin finds the interpreter too
    assert "VERSION 1.0" in _run(moved / "runtime" / "bin" / "demo").stdout
    with tarfile.open(root / "dist" / f"demo-app-1.0-{NATIVE}.tar.gz") as tf:
        launcher = tf.getmember(f"demo-app-{NATIVE}/demo")
        assert launcher.mode == 0o755


@needs_linux
@pytest.mark.slow
@pytest.mark.network
def test_onefile_cache_concurrency_and_sweep(tmp_path, shared_cache, monkeypatch):
    monkeypatch.setenv("PYXBUILD_CACHE_DIR", str(shared_cache))
    root = _project(tmp_path / "p", mode="onefile")
    _build(root)
    exe = root / "dist" / "demo"
    cache = tmp_path / "cache"
    env = {"PYX_ONEFILE_DIR": str(cache)}

    # four simultaneous first starts extract once
    procs = [subprocess.Popen([str(exe), str(i)], stdout=subprocess.PIPE, text=True,
                              env={**os.environ, **env}) for i in range(4)]
    outs = [p.communicate(timeout=300)[0] for p in procs]
    assert all(p.returncode == 0 and "VERSION 1.0" in o for p, o in zip(procs, outs, strict=True))
    versions = [d for d in (cache / "demo-app").iterdir() if not d.name.startswith(".")]
    assert len(versions) == 1
    marker = versions[0] / ".pyx-complete"
    stamp = marker.stat().st_mtime_ns
    assert "VERSION 1.0" in _run(exe, env=env).stdout
    assert marker.stat().st_mtime_ns == stamp  # not extracted again

    # old versions: the one in use and the newest other one stay
    appdir = cache / "demo-app"
    for name, age in (("aaaaaaaaaaaa", 300), ("bbbbbbbbbbbb", 200), ("cccccccccccc", 100)):
        (appdir / name).mkdir()
        (appdir / name / ".pyx-complete").write_text("x\n")
        (appdir / name / ".pyx-lock").write_text("")
        os.utime(appdir / name / ".pyx-complete", (1e9 - age, 1e9 - age))
    import fcntl

    with open(appdir / "aaaaaaaaaaaa" / ".pyx-lock") as held:  # a running copy
        fcntl.flock(held, fcntl.LOCK_SH)
        root2 = _project(tmp_path / "p", version="2.0", mode="onefile")
        _build(root2)
        assert "VERSION 2.0" in _run(exe, env=env).stdout
    left = sorted(d.name for d in appdir.iterdir() if not d.name.startswith("."))
    assert "aaaaaaaaaaaa" in left        # in use
    assert "bbbbbbbbbbbb" not in left    # removed
    assert len(left) == 3                # in use + newest previous (1.0) + 2.0


@needs_linux
@pytest.mark.slow
@pytest.mark.network
def test_onefile_temp_mode_cleans_up(tmp_path, shared_cache, monkeypatch):
    monkeypatch.setenv("PYXBUILD_CACHE_DIR", str(shared_cache))
    root = _project(tmp_path / "p", mode="onefile", extract="temp")
    _build(root)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    r = _run(root / "dist" / "demo", "--sleep", "0", env={"TMPDIR": str(tmp)})
    assert r.returncode == 0 and "VERSION 1.0" in r.stdout and str(tmp) in r.stdout
    assert list(tmp.iterdir()) == []



def test_case_clash_reports_the_on_disk_spelling(tmp_path):
    from pyxbuild.install import _case_clash

    (tmp_path / "lib" / "Crypto").mkdir(parents=True)
    (tmp_path / "lib" / "Crypto" / "__init__.py").write_text("")
    assert _case_clash(tmp_path, "lib/crypto/__init__.py") == "lib/Crypto/__init__.py"
    assert _case_clash(tmp_path, "lib/Crypto/__init__.py") is None
    assert _case_clash(tmp_path, "lib/other/x.py") is None
