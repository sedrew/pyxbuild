"""Patch executables: only the changes between two onefile builds."""

import hashlib
import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

from conftest import ROOT, make_wheel
from pyxbuild.config import EntryPoint
from pyxbuild.errors import PyxbuildError
from pyxbuild.launcher import write_onefile
from pyxbuild.onefile import write_archive
from pyxbuild.patch import make_patch, patch_payload


def _apply_module():
    path = ROOT / "src" / "pyxbuild" / "_boot" / "_pyxbuild_patch.py"
    spec = importlib.util.spec_from_file_location("_pyxbuild_patch_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree(root: Path, version: str, files: dict[str, bytes]) -> Path:
    files = {**files, "runtime/pyxbuild.json": json.dumps(
        {"project": {"name": "demo", "version": version}, "python": "3.12.13"}).encode()}
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root


def _onefile(tmp: Path, name: str, files: dict[str, bytes], version: str) -> Path:
    tree = _tree(tmp / f"{name}-tree", version, files)
    archive = write_archive(tree, tmp / f"{name}.pyxarc")
    exe = tmp / f"{name}.exe"
    write_onefile(exe, EntryPoint("demo", "demo", "main", gui=False), "win_amd64", archive,
                  app="demo")
    return exe


COMMON = {
    "runtime/python.exe": b"MZ" + os.urandom(20000),
    "runtime/Lib/os.py": b"# os\n" * 2000,
    "runtime/Lib/site-packages/requests/__init__.py": b"# requests 2.32\n" * 300,
}


@pytest.fixture
def versions(tmp_path):
    v1 = _onefile(tmp_path, "v1", {**COMMON,
                                   "runtime/Lib/site-packages/demo/__init__.py": b"V = 1\n"}, "1.0")
    v2 = _onefile(tmp_path, "v2", {
        **COMMON,
        "runtime/Lib/site-packages/demo/__init__.py": b"V = 2\n",
        "runtime/Lib/site-packages/demo/new.py": b"NEW = True\n",
        # same content, new path: still reused from the old version
        "runtime/Lib/copy_of_os.py": COMMON["runtime/Lib/os.py"],
    }, "2.0")
    return v1, v2


def _install(exe: Path, appdir: Path) -> Path:
    """What the onefile launcher leaves in the cache after a first start."""
    from pyxbuild.launcher import decode_manifest
    from pyxbuild.onefile import read_archive

    data = exe.read_bytes()
    m = decode_manifest(data)
    folder = appdir / m["onefile"][:12]
    off, size = int(m["archive-offset"]), int(m["archive-size"])
    for rel, content in read_archive(data[off:off + size]).items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_bytes(content)
    (folder / ".pyx-complete").write_text(m["onefile"] + "\n")
    return folder


def _payload_dir(patch: Path, dest: Path) -> Path:
    _, files = patch_payload(patch)
    for rel, content in files.items():
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_bytes(content)
    return dest


def test_patch_carries_only_changes(versions, tmp_path):
    v1, v2 = versions
    info = make_patch(v1, v2, tmp_path / "update.exe")
    manifest, payload = patch_payload(info.path)
    patch = json.loads(payload["patch.json"])
    by_path = {e["path"]: e for e in patch["files"]}
    assert "runtime/python.exe" in patch["keep"] and "runtime/python.exe" not in by_path
    assert by_path["runtime/Lib/copy_of_os.py"] == {
        **by_path["runtime/Lib/copy_of_os.py"], "from": "base", "src": "runtime/Lib/os.py"}
    assert by_path["runtime/Lib/site-packages/demo/new.py"]["from"] == "payload"
    blobs = {k for k in payload if k.startswith("blobs/")}
    assert len(blobs) == 3  # demo/__init__.py, demo/new.py, pyxbuild.json
    assert manifest["patch-base"] and manifest["app"] == "demo"
    from pyxbuild.launcher import stub_bytes

    assert info.carried_bytes < 300  # the three small changed files
    assert info.size < len(stub_bytes("win_amd64", False, onefile=True)) + 8 * 1024
    assert (info.reused, info.carried, info.fetched) == (4, 3, 0)


def test_patch_rejects_unrelated_or_identical(versions, tmp_path):
    v1, v2 = versions
    with pytest.raises(PyxbuildError, match="same version"):
        make_patch(v1, v1, tmp_path / "u.exe")
    (tmp_path / "o").mkdir()
    other = _onefile(tmp_path / "o", "x", {"a.txt": b"a"}, "1")
    data = other.read_bytes().replace(b"app=demo\n", b"app=othr\n")
    other.write_bytes(data)
    with pytest.raises(PyxbuildError, match="different applications"):
        make_patch(v1, other, tmp_path / "u.exe")


def test_apply_builds_the_new_version_next_to_the_old(versions, tmp_path):
    v1, v2 = versions
    appdir = tmp_path / "cache" / "demo"
    old = _install(v1, appdir)
    patch = make_patch(v1, v2, tmp_path / "update.exe").path
    payload = _payload_dir(patch, tmp_path / "payload")
    mod = _apply_module()
    new = Path(mod.apply(str(payload), str(appdir), log=lambda *a: None))

    expected = _install(v2, tmp_path / "expected" / "demo")  # what the full exe extracts
    assert new.name == expected.name
    got = {p.relative_to(new).as_posix(): p.read_bytes() for p in new.rglob("*") if p.is_file()}
    want = {p.relative_to(expected).as_posix(): p.read_bytes()
            for p in expected.rglob("*") if p.is_file()}
    assert got == want
    assert (old / ".pyx-next").read_text().strip() == new.name
    assert os.stat(new / "runtime" / "python.exe").st_ino == \
        os.stat(old / "runtime" / "python.exe").st_ino  # hard link, no copy
    assert not [p for p in appdir.iterdir() if ".tmp-" in p.name]
    # applying again is harmless
    assert Path(mod.apply(str(payload), str(appdir), log=lambda *a: None)) == new


def test_apply_refuses_a_damaged_base(versions, tmp_path):
    v1, v2 = versions
    appdir = tmp_path / "cache" / "demo"
    old = _install(v1, appdir)
    (old / "runtime" / "Lib" / "os.py").write_bytes(b"tampered")
    payload = _payload_dir(make_patch(v1, v2, tmp_path / "u.exe").path, tmp_path / "p")
    mod = _apply_module()
    with pytest.raises(mod.PatchError, match="damaged"):
        mod.apply(str(payload), str(appdir), log=lambda *a: None)
    assert sorted(p.name for p in appdir.iterdir()) == [old.name]  # nothing half-built
    assert not (old / ".pyx-next").exists()


def test_apply_without_the_base_installed(versions, tmp_path):
    v1, v2 = versions
    payload = _payload_dir(make_patch(v1, v2, tmp_path / "u.exe").path, tmp_path / "p")
    (tmp_path / "cache" / "demo").mkdir(parents=True)
    mod = _apply_module()
    with pytest.raises(mod.PatchError, match="not installed"):
        mod.apply(str(payload), str(tmp_path / "cache" / "demo"), log=lambda *a: None)


def test_fetch_public_downloads_wheel_files(versions, tmp_path):
    v1, _ = versions
    module = b"# idna 3.10\n" * 500
    wheel = make_wheel(tmp_path / "wheels", "idna", "3.10",
                       files={"idna/__init__.py": module})
    v3 = _onefile(tmp_path, "v3", {**COMMON,
                                   "runtime/Lib/site-packages/demo/__init__.py": b"V = 3\n",
                                   "runtime/Lib/site-packages/idna/__init__.py": module}, "3.0")
    sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    public = [(wheel.as_uri(), sha, wheel.stat().st_size, wheel)]
    info = make_patch(v1, v3, tmp_path / "u.exe", wheels=public)
    assert info.fetched == 1
    _, payload = patch_payload(info.path)
    assert module not in payload.values()  # not carried

    appdir = tmp_path / "cache" / "demo"
    _install(v1, appdir)
    mod = _apply_module()
    new = Path(mod.apply(str(_payload_dir(info.path, tmp_path / "p")), str(appdir),
                         log=lambda *a: None))
    assert (new / "runtime/Lib/site-packages/idna/__init__.py").read_bytes() == module

    # a wheel that does not match its hash is refused
    shutil.copy(wheel, tmp_path / "evil.whl")
    with open(tmp_path / "evil.whl", "ab") as f:
        f.write(b"x")
    bad = [(Path(tmp_path / "evil.whl").as_uri(), sha, None, wheel)]
    info = make_patch(v1, v3, tmp_path / "u2.exe", wheels=bad)
    appdir2 = tmp_path / "cache2" / "demo"
    _install(v1, appdir2)
    with pytest.raises(mod.PatchError, match="sha256 mismatch"):
        mod.apply(str(_payload_dir(info.path, tmp_path / "p2")), str(appdir2),
                  log=lambda *a: None)


# --- end to end (Windows or Wine, network) ---------------------------------

APP_CODE = '''
import sys
import idna

def main():
    print("VERSION", "{version}", "idna", idna.__version__)
    return 0
'''


def _app_project(root: Path, version: str, idna: str) -> Path:
    import textwrap

    (root / "demoapp").mkdir(parents=True, exist_ok=True)
    (root / "demoapp" / "__init__.py").write_text(APP_CODE.replace("{version}", version))
    (root / "pyproject.toml").write_text(textwrap.dedent(f"""
        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"
        [project]
        name = "demoapp"
        version = "{version}"
        dependencies = ["idna=={idna}"]
        [project.scripts]
        demoapp = "demoapp:main"
        [tool.pyxbuild]
        python = "3.12"
        mode = "onefile"
    """))
    return root


def _win(path: Path) -> str:
    import sys

    return str(path) if sys.platform == "win32" else "Z:" + str(path).replace("/", "\\")


@pytest.mark.slow
@pytest.mark.network
def test_patch_exe_end_to_end(tmp_path, shared_cache):
    from conftest import run_exe, wine_available
    from pyxbuild import cli

    if not wine_available():
        pytest.skip("needs Windows or Wine")
    old_cache = os.environ.get("PYXBUILD_CACHE_DIR")
    os.environ["PYXBUILD_CACHE_DIR"] = str(shared_cache)
    try:
        proj = _app_project(tmp_path / "proj", "1.0", "3.7")
        assert cli.main(["build", str(proj), "-q"]) == 0
        v1 = tmp_path / "v1.exe"
        shutil.copy(proj / "dist" / "demoapp.exe", v1)
        _app_project(proj, "2.0", "3.10")
        assert cli.main(["build", str(proj), "-q"]) == 0
        v2 = proj / "dist" / "demoapp.exe"
        update = tmp_path / "update.exe"
        assert cli.main(["patch", "--from", str(v1), "--to", str(v2), "-o", str(update),
                         "--fetch-public", "--lock", str(proj / "pylock.win_amd64.toml")]) == 0
    finally:
        if old_cache is None:
            os.environ.pop("PYXBUILD_CACHE_DIR", None)
        else:
            os.environ["PYXBUILD_CACHE_DIR"] = old_cache
    assert update.stat().st_size < 64 * 1024, update.stat().st_size  # stub + app changes

    env = {"PYX_ONEFILE_DIR": _win(tmp_path / "pyx")}
    first = run_exe(v1, env=env)
    assert first.returncode == 0, first.stderr
    assert "VERSION 1.0 idna 3.7" in first.stdout

    applied = run_exe(update, env=env)
    assert applied.returncode == 0, applied.stderr + applied.stdout
    assert "updated demoapp to 2.0" in applied.stdout
    assert "downloading idna-3.10" in applied.stdout

    again = run_exe(v1, env=env)  # the old executable now runs the new version
    assert again.returncode == 0, again.stderr
    assert "VERSION 2.0 idna 3.10" in again.stdout

    from pyxbuild.launcher import decode_manifest

    target = tmp_path / "pyx" / "demoapp" / decode_manifest(v2.read_bytes())["onefile"][:12]
    stamp = (target / ".pyx-complete").stat().st_mtime_ns
    full = run_exe(v2, env=env)  # the new full executable finds its version ready
    assert full.returncode == 0 and "VERSION 2.0 idna 3.10" in full.stdout
    assert (target / ".pyx-complete").stat().st_mtime_ns == stamp

    # a patch for a version that is not installed explains itself
    elsewhere = run_exe(update, env={"PYX_ONEFILE_DIR": _win(tmp_path / "empty")})
    assert elsewhere.returncode != 0
    assert "not installed here" in elsewhere.stderr
