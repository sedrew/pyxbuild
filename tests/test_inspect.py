"""``pyxbuild inspect``, ``[tool.pyxbuild] require`` and the log of dropped files."""

import json
import shutil
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import GOLDEN, make_wheel
from pyxbuild import cli
from pyxbuild.builder import BuildOptions, _log_dropped, missing_required, write_tarball, write_zip
from pyxbuild.config import EntryPoint, load_config
from pyxbuild.errors import ConfigError, PyxbuildError
from pyxbuild.inspector import Contents, extract, matches, open_contents
from pyxbuild.install import wheel_site_files
from pyxbuild.launcher import build_launcher, decode_manifest, stub_bytes, write_onefile
from pyxbuild.onefile import MAGIC, write_archive
from pyxbuild.patch import make_patch
from pyxbuild.targets import get_target

SITE = "runtime/Lib/site-packages"
FILES = {
    f"{SITE}/mypkg/__init__.py": b"",
    f"{SITE}/mypkg/data/a.yaml": b"a: 1\n",
    f"{SITE}/mypkg/data/b.yaml": b"b: 2\n" * 1000,
    f"{SITE}/mypkg/data/notes.txt": b"notes\n",
    "runtime/python.exe": b"MZ" + b"\0" * 5000,
}


def _tree(root: Path, files: dict[str, bytes], version: str = "1.0") -> Path:
    files = {**files, "runtime/pyxbuild.json": json.dumps(
        {"pyxbuild": "0", "project": {"name": "demo", "version": version},
         "target": "win_amd64", "python": "3.12.13",
         "launchers": {"demo": {"entry-point": "demo:main", "gui": False}}}).encode()}
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root


def _onefile(tmp: Path, name: str, files: dict[str, bytes], version: str = "1.0") -> Path:
    tree = _tree(tmp / f"{name}-tree", files, version)
    archive = write_archive(tree, tmp / f"{name}.pyxarc")
    exe = tmp / f"{name}.exe"
    write_onefile(exe, EntryPoint("demo", "demo", "main", gui=False), "win_amd64", archive,
                  app="demo")
    return exe


def _inspect(capsys, *args) -> str:
    capsys.readouterr()
    assert cli.main(["inspect", *map(str, args)]) == 0
    return capsys.readouterr().out


# --- inspect ------------------------------------------------------------------

def test_inspect_onefile_summary_list_extract(tmp_path, capsys):
    exe = _onefile(tmp_path, "app", FILES)
    # the launcher itself contains the archive magic: inspect must locate the
    # archive through the manifest, not by searching for it
    assert MAGIC in stub_bytes("win_amd64", False, onefile=True)

    out = _inspect(capsys, exe)
    assert "onefile application (win_amd64)" in out
    assert "project:  demo 1.0" in out and "entry:    demo:main" in out
    assert f"files:    {len(FILES) + 1} " in out
    assert f"sha256 {decode_manifest(exe.read_bytes())['onefile']}" in out
    assert "warning" not in out

    out = _inspect(capsys, exe, "--list", "*/mypkg/data/*")
    lines = out.splitlines()
    assert lines[:-1] == [f"   5  {SITE}/mypkg/data/a.yaml", f"5000  {SITE}/mypkg/data/b.yaml",
                          f"   6  {SITE}/mypkg/data/notes.txt"]
    assert lines[-1].startswith("3 file(s)")
    # several globs; a folder name lists what is below it
    out = _inspect(capsys, exe, "--list", "*.txt", "runtime/pyxbuild.json")
    assert [line.split()[-1] for line in out.splitlines()[:-1]] == [
        f"{SITE}/mypkg/data/notes.txt", "runtime/pyxbuild.json"]
    out = _inspect(capsys, exe, "--list", f"{SITE}/mypkg")
    assert out.splitlines()[-1].startswith("4 file(s)")

    dest = tmp_path / "out"
    out = _inspect(capsys, exe, "--extract", dest, "*/mypkg/data/*.yaml")
    assert "extracted 2 file(s)" in out
    got = {p.relative_to(dest).as_posix(): p.read_bytes() for p in dest.rglob("*")
           if p.is_file()}
    assert got == {k: v for k, v in FILES.items() if k.endswith(".yaml")}

    everything = tmp_path / "all"
    _inspect(capsys, exe, "--extract", everything)
    assert (everything / "runtime/pyxbuild.json").is_file()
    assert len([p for p in everything.rglob("*") if p.is_file()]) == len(FILES) + 1


def test_inspect_errors(tmp_path, capsys):
    exe = _onefile(tmp_path, "app", FILES)
    folder_launcher = tmp_path / "folder.exe"
    folder_launcher.write_bytes(build_launcher(EntryPoint("demo", "demo", "main", False),
                                               "win_amd64", python_rel="runtime"))
    with pytest.raises(PyxbuildError, match="launcher of a folder build"):
        open_contents(folder_launcher)
    (tmp_path / "x.txt").write_text("hello")
    with pytest.raises(PyxbuildError, match="not a pyxbuild"):
        open_contents(tmp_path / "x.txt")
    with pytest.raises(PyxbuildError, match="not a pyxbuild application folder"):
        open_contents(tmp_path)
    capsys.readouterr()
    assert cli.main(["inspect", str(exe), "--extract", str(tmp_path / "e"), "*.nope"]) == 1
    assert "no file matches" in capsys.readouterr().err


def test_inspect_folder_and_zip(tmp_path, capsys):
    app = _tree(tmp_path / "demo-win_amd64", FILES)
    out = _inspect(capsys, app)
    assert "folder application (win_amd64)" in out and "scripts:  demo" in out
    zip_path = tmp_path / "demo-1.0-win_amd64.zip"
    write_zip(app, zip_path, 315532800)
    out = _inspect(capsys, zip_path)
    assert "zip application (win_amd64)" in out and "sha256:" in out
    # the zip's top folder is not part of the paths
    out = _inspect(capsys, zip_path, "--list", "*/mypkg/data/*.yaml")
    assert out.splitlines()[0].endswith(f"{SITE}/mypkg/data/a.yaml")
    dest = tmp_path / "x"
    _inspect(capsys, zip_path, "--extract", dest, "*notes.txt")
    assert (dest / SITE / "mypkg/data/notes.txt").read_bytes() == b"notes\n"
    tar_path = tmp_path / "demo-1.0-linux_x86_64.tar.gz"
    write_tarball(app, tar_path, 315532800)
    assert "tar application" in _inspect(capsys, tar_path)
    out = _inspect(capsys, tar_path, "--list", "*.yaml")
    assert out.splitlines()[-1].startswith("2 file(s)")


def test_inspect_patch(tmp_path, capsys):
    v1 = _onefile(tmp_path, "v1", FILES, "1.0")
    v2_files = {k: v for k, v in FILES.items() if not k.endswith("notes.txt")}
    v2_files[f"{SITE}/mypkg/data/a.yaml"] = b"a: 2\n"
    v2_files[f"{SITE}/mypkg/data/c.yaml"] = b"c: 3\n"
    v2 = _onefile(tmp_path, "v2", v2_files, "2.0")
    m1 = decode_manifest(v1.read_bytes())
    patch = make_patch(v1, v2, tmp_path / "demo-update-2.0.exe").path

    out = _inspect(capsys, patch)
    assert "patch for demo" in out
    assert f"applies to: {m1['onefile'][:12]} (version 1.0)" in out
    assert "updates to: 2.0" in out
    assert "added:      1 file(s)" in out
    assert "replaced:   2 file(s)" in out  # a.yaml and runtime/pyxbuild.json
    assert "removed:    1 file(s)" in out

    out = _inspect(capsys, patch, "--list", "*/mypkg/*")
    assert out.splitlines()[:-1] == [
        f"M 5  {SITE}/mypkg/data/a.yaml  (carried)",
        f"A 5  {SITE}/mypkg/data/c.yaml  (carried)",
        f"D    {SITE}/mypkg/data/notes.txt",
    ]
    # --extract writes the payload as it travels
    dest = tmp_path / "payload"
    _inspect(capsys, patch, "--extract", dest)
    assert (dest / "patch.json").is_file() and (dest / "_pyxbuild_patch.py").is_file()


def test_extract_refuses_paths_outside_the_folder(tmp_path):
    for bad in ("../evil", "/abs", "C:/x", "a/../../b"):
        c = Contents(tmp_path, "onefile", {bad: 1}, lambda rel: b"x")
        with pytest.raises(PyxbuildError, match="outside the target folder"):
            extract(c, tmp_path / "dest", [])
    assert not (tmp_path / "evil").exists()


def test_glob_matching():
    assert matches(f"{SITE}/mypkg/data/a.yaml", ["*/mypkg/data/*"])
    assert matches(f"{SITE}/mypkg/data/a.yaml", ["runtime/Lib"])
    assert not matches(f"{SITE}/mypkg/data/a.yaml", ["mypkg/data/*"])
    assert matches("anything", [])


# --- require ------------------------------------------------------------------

PROJECT = """
[project]
name = "demo"
version = "1.0"
[project.scripts]
demo = "demo:main"
"""


def test_require_config(tmp_path):
    (tmp_path / "pyproject.toml").write_text(PROJECT)
    assert load_config(tmp_path).require == []
    (tmp_path / "pyproject.toml").write_text(
        PROJECT + '[tool.pyxbuild]\nrequire = ["mypkg/data/*.yaml", "myapp/templates/**"]\n')
    assert load_config(tmp_path).require == ["mypkg/data/*.yaml", "myapp/templates/**"]
    for bad in ('"mypkg"', '[1]', '["../x"]', '["a\\\\b"]', '[""]', '["C:/x"]'):
        (tmp_path / "pyproject.toml").write_text(PROJECT + f"[tool.pyxbuild]\nrequire = {bad}\n")
        with pytest.raises(ConfigError, match="require"):
            load_config(tmp_path)


def _app(tmp_path: Path, wheels: dict[str, list[str]], installed: list[str],
         require: list[str], exclude: list[str] | None = None):
    """An assembled app with ``installed`` in site-packages, built from ``wheels``."""
    paths = []
    for name, files in wheels.items():
        paths.append(make_wheel(tmp_path / "wheels", name, files={f: "x" for f in files}))
    app = tmp_path / "app"
    for rel in installed:
        (app / SITE / rel).parent.mkdir(parents=True, exist_ok=True)
        (app / SITE / rel).write_text("x")
    (tmp_path / "pyproject.toml").write_text(PROJECT)
    cfg = load_config(tmp_path)
    cfg.require, cfg.exclude = require, exclude or []
    tb = SimpleNamespace(target=get_target("win_amd64"), python="3.12", wheels=paths[1:],
                         local_wheels=[], project_wheel=SimpleNamespace(path=paths[0]))
    return cfg, tb, app


def test_missing_required(tmp_path):
    cfg, tb, app = _app(
        tmp_path,
        {"demo": ["demo/__init__.py", "demo/templates/a.html"],
         "mypkg": ["mypkg/__init__.py", "mypkg/data/a.txt", "mypkg/data/secret.yaml"]},
        installed=["demo/__init__.py", "demo/templates/a.html", "mypkg/__init__.py",
                   "mypkg/data/a.txt"],
        require=["demo/templates/**", "mypkg/data/*.txt", "mypkg/data/*.yaml",
                 "mypkg/schemas/*.json", "other/x.cfg"],
        exclude=["*.yaml"])
    assert missing_required(cfg, tb, app) == [
        "'mypkg/data/*.yaml' matches no file: mypkg-1.0-py3-none-any.whl has "
        "'mypkg/data/secret.yaml', left out by exclude/slim pattern '*.yaml'",
        "'mypkg/schemas/*.json' matches no file: expected from mypkg-1.0-py3-none-any.whl, "
        "which installs mypkg/ but has no such file",
        "'other/x.cfg' matches no file: no wheel of the application has such a file",
    ]
    cfg.require = ["demo/templates/**", "mypkg/data/*.txt"]
    assert missing_required(cfg, tb, app) == []


def test_wheel_site_files_and_dropped_log(tmp_path, capsys):
    wheel = make_wheel(tmp_path, "mypkg", files={
        "mypkg/__init__.py": "", "mypkg/data/a.yaml": "a",
        "mypkg-1.0.data/purelib/extra/x.py": "", "mypkg-1.0.data/scripts/tool": "#!"})
    files = wheel_site_files(wheel)
    assert "extra/x.py" in files and "mypkg/data/a.yaml" in files
    assert not any("scripts" in f or ".data/" in f for f in files)

    tb = SimpleNamespace(target=get_target("win_amd64"), local_wheels=[wheel],
                         project_wheel=SimpleNamespace(path=make_wheel(tmp_path, "demo")))
    _log_dropped(BuildOptions(), tb, {"mypkg/data/a.yaml", "other/z.py"})
    err = capsys.readouterr().err
    assert "[win_amd64]   mypkg-1.0-py3-none-any.whl: mypkg/data/a.yaml" in err
    assert "demo" not in err and "other/z.py" not in err


# --- end to end: golden project "features" (local package featdata with a .yaml) ---

def _features(tmp: Path) -> Path:
    proj = tmp / "features"
    shutil.copytree(GOLDEN / "features", proj,
                    ignore=shutil.ignore_patterns("dist", "pylock.*.toml", "__pycache__"))
    return proj


def _build(proj: Path, cache: Path, monkeypatch, *args: str) -> int:
    monkeypatch.setenv("PYXBUILD_CACHE_DIR", str(cache))
    return cli.main(["build", str(proj), *args])


@pytest.mark.slow
@pytest.mark.network
def test_e2e_inspect_onefile_and_require_passes(tmp_path, shared_cache, monkeypatch, capsys):
    proj = _features(tmp_path)
    assert load_config(proj).require  # the golden project requires its data files
    assert _build(proj, shared_cache, monkeypatch, "--mode", "onefile", "-q") == 0
    exe = proj / "dist" / "features.exe"

    out = _inspect(capsys, exe)
    assert "onefile application (win_amd64)" in out and "project:  features 0.3.0" in out
    out = _inspect(capsys, exe, "--list", "*/featdata/data/*", "*/features/data/*")
    assert [line.split()[-1] for line in out.splitlines()[:-1]] == [
        f"{SITE}/featdata/data/settings.yaml", f"{SITE}/features/data/greeting.txt"]
    dest = tmp_path / "x"
    _inspect(capsys, exe, "--extract", dest, "*/featdata/data/*")
    src = proj / "packages/featdata/src/featdata/data/settings.yaml"
    assert (dest / SITE / "featdata/data/settings.yaml").read_bytes() == src.read_bytes()


@pytest.mark.slow
@pytest.mark.network
def test_e2e_require_fails_when_the_wheel_lacks_the_file(tmp_path, shared_cache, monkeypatch,
                                                         capsys):
    proj = _features(tmp_path)
    # the local package's wheel is built from an incomplete tree: no .yaml in it
    (proj / "packages/featdata/pyproject.toml").write_text(
        (proj / "packages/featdata/pyproject.toml").read_text()
        + textwrap.dedent("""
            [tool.hatch.build.targets.wheel]
            exclude = ["*.yaml"]
        """))
    assert _build(proj, shared_cache, monkeypatch, "--mode", "onefile") == 1
    err = capsys.readouterr().err
    assert "required files are missing" in err
    assert ("'featdata/data/*.yaml' matches no file: expected from "
            "featdata-1.0-py3-none-any.whl, which installs featdata/ but has no such file") in err
    assert "hint: patterns are relative to site-packages" in err
    assert not (proj / "dist" / "features.exe").exists()


@pytest.mark.slow
@pytest.mark.network
def test_e2e_require_fails_when_excluded_and_logs_dropped(tmp_path, shared_cache, monkeypatch,
                                                          capsys):
    proj = _features(tmp_path)
    text = (proj / "pyproject.toml").read_text()
    (proj / "pyproject.toml").write_text(
        text.replace('exclude = ["*.pyi"]', 'exclude = ["*.pyi", "featdata/data/"]'))
    assert _build(proj, shared_cache, monkeypatch) == 1
    err = capsys.readouterr().err
    # the files exclude/slim dropped from local wheels are named, not just counted
    assert "excluded 2 file(s)" in err
    assert "  featdata-1.0-py3-none-any.whl: featdata/data/settings.yaml" in err
    assert "  features-0.3.0-py3-none-any.whl: features/typing_only.pyi" in err
    assert ("featdata-1.0-py3-none-any.whl has 'featdata/data/settings.yaml', left out by "
            "exclude/slim pattern 'featdata/data/'") in err
