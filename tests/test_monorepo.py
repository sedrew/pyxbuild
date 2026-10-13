"""Monorepos: non-ASCII metadata, uv workspace members and path sources,
find-links outside the project folder."""

import os
import shutil
import textwrap
import zipfile
from pathlib import Path

import pytest

from conftest import make_wheel, run_exe, wine_available
from pyxbuild import cli
from pyxbuild.builder import _absolute_url
from pyxbuild.errors import PyxbuildError
from pyxbuild.local import _Sources, workspace_members
from pyxbuild.lock import Lock, LockedPackage, _localize_files, read_lock, select_files
from pyxbuild.project import read_wheel_metadata
from pyxbuild.resources import RT_VERSION, add_resources, launcher_resources
from pyxbuild.targets import WIN_AMD64

# --- non-ASCII project metadata -------------------------------------------


def _wheel_with_summary(tmp_path, summary):
    path = tmp_path / "app-1.0-py3-none-any.whl"
    meta = (f"Metadata-Version: 2.4\nName: app\nVersion: 1.0\nSummary: {summary}\n"
            "Requires-Dist: requests; extra == \"сеть\"\n")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("app-1.0.dist-info/METADATA", meta.encode("utf-8"))
    return path


def test_metadata_with_cyrillic_summary_is_text(tmp_path):
    name, version, requires, summary = read_wheel_metadata(
        _wheel_with_summary(tmp_path, "Тестовое приложение"))
    assert summary == "Тестовое приложение" and isinstance(summary, str)
    assert requires == ['requests; extra == "сеть"']


def test_version_info_with_cyrillic_description(tmp_path):
    from pyxbuild.launcher import stub_bytes

    *_, summary = read_wheel_metadata(_wheel_with_summary(tmp_path, "Тест 测试"))
    res = launcher_resources(script="app", project_name="app", version="1.0",
                             description=summary, version_info_cfg={}, icon=None)
    exe = add_resources(stub_bytes("win_amd64", gui=False), res)
    assert "Тест 测试".encode("utf-16-le") in res[RT_VERSION][1]
    assert "Тест 测试".encode("utf-16-le") in exe


# --- find-links above the project -----------------------------------------


def test_find_links_above_the_lock_become_relative(tmp_path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "x-1-py3-none-any.whl").write_bytes(b"wheel")
    app = tmp_path / "apps" / "app"
    app.mkdir(parents=True)
    text = ('lock-version = "1.0"\n[[packages]]\nname = "x"\nwheels = [{ url = "'
            + (wheels / "x-1-py3-none-any.whl").as_uri() + '", hashes = {} }]\n')
    out = _localize_files(text, app)
    assert 'path = "../../wheels/x-1-py3-none-any.whl"' in out
    assert "file://" not in out
    (app / "pylock.toml").write_text(out)
    (pkg,) = read_lock(app / "pylock.toml").packages
    assert Path(_absolute_url(pkg.wheels[0].url, app)) == (wheels / "x-1-py3-none-any.whl"
                                                           ).resolve()


# --- workspace members and path sources (no building) ---------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def test_workspace_and_path_sources_are_found(tmp_path):
    _write(tmp_path / "pyproject.toml", """
        [tool.uv.workspace]
        members = ["packages/*"]
        exclude = ["packages/old"]
        [tool.uv.sources]
        util = { workspace = true }
    """)
    for name in ("app", "core", "util", "old"):
        _write(tmp_path / "packages" / name / "pyproject.toml",
               f'[project]\nname = "{name.upper()}"\nversion = "1"\n')
    _write(tmp_path / "packages" / "app" / "pyproject.toml", """
        [project]
        name = "app"
        version = "1"
        [tool.uv.sources]
        Core = { workspace = true }
        tools = { path = "../../extra/tools" }
        requests = { index = "internal" }
    """)
    assert set(workspace_members(tmp_path)) == {"app", "core", "util"}
    sources = _Sources(tmp_path / "packages" / "app")
    assert sources.get("core") == (tmp_path / "packages" / "core").resolve()
    assert sources.get("util") == (tmp_path / "packages" / "util").resolve()  # root's sources
    assert sources.get("tools") == (tmp_path / "extra" / "tools").resolve()
    assert sources.get("requests") is None  # index sources stay with the resolver


def test_workspace_source_without_member_is_an_error(tmp_path):
    _write(tmp_path / "pyproject.toml", '[tool.uv.workspace]\nmembers = ["packages/*"]\n')
    _write(tmp_path / "packages" / "app" / "pyproject.toml", """
        [project]
        name = "app"
        version = "1"
        [tool.uv.sources]
        ghost = { workspace = true }
    """)
    with pytest.raises(PyxbuildError, match="no member .* named ghost"):
        _Sources(tmp_path / "packages" / "app")


def test_directory_entries_of_a_foreign_lock_are_selected_for_building(tmp_path):
    lock = Lock(tmp_path / "pylock.toml", (
        LockedPackage("app", "1", None, (), None, "directory", directory="."),
        LockedPackage("core", "1", None, (), None, "directory", directory="packages/core"),
        LockedPackage("gitpkg", "1", None, (), None, "vcs"),
    ), {})
    env = WIN_AMD64.marker_environment("3.12.0")
    with pytest.raises(Exception, match="gitpkg: vcs sources are not supported"):
        select_files(lock, WIN_AMD64, env, WIN_AMD64.tags("3.12"), project="app")
    lock = Lock(lock.path, lock.packages[:2], {})
    [sel] = select_files(lock, WIN_AMD64, env, WIN_AMD64.tags("3.12"), project="app")
    assert sel.file is None and sel.package.directory == "packages/core"


# --- end to end: a uv workspace monorepo ----------------------------------


def _monorepo(root: Path) -> Path:
    _write(root / "pyproject.toml", """
        [tool.uv.workspace]
        members = ["packages/*"]
        [tool.uv.sources]
        util = { workspace = true }
    """)
    pkgs = {
        "app": ('dependencies = ["core", "tools", "extraw"]\n'
                'description = "Тестовое приложение"\n'
                '[project.scripts]\napp = "app:main"\n'
                '[tool.uv.sources]\ncore = { workspace = true }\n'
                'tools = { path = "../../extra/tools" }\n'
                '[tool.pyxbuild]\npython = "3.12"\nfind-links = ["../../wheels"]\n',
                "def main():\n    import core, tools, util, extraw\n"
                "    print('MONO', core.VALUE, tools.VALUE, util.VALUE, extraw.VALUE)\n"),
        "core": ('dependencies = ["util", "idna"]\n', "import idna\nVALUE = 'core'\n"),
        "util": ("dependencies = []\n", "VALUE = 'util'\n"),
    }
    for name, (extra, code) in pkgs.items():
        _write(root / "packages" / name / "pyproject.toml",
               f'[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
               f'[project]\nname = "{name}"\nversion = "1.0"\n{extra}')
        _write(root / "packages" / name / name / "__init__.py", code)
    _write(root / "extra" / "tools" / "pyproject.toml",
           '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
           '[project]\nname = "tools"\nversion = "2.0"\ndependencies = []\n')
    _write(root / "extra" / "tools" / "tools" / "__init__.py", "VALUE = 'tools'\n")
    make_wheel(root / "wheels", "extraw", files={"extraw/__init__.py": "VALUE = 'extraw'\n"})
    return root / "packages" / "app"


@pytest.mark.slow
@pytest.mark.network
def test_uv_workspace_monorepo_builds_and_runs(tmp_path, shared_cache):
    app = _monorepo(tmp_path / "repo")
    old = os.environ.get("PYXBUILD_CACHE_DIR")
    os.environ["PYXBUILD_CACHE_DIR"] = str(shared_cache)
    try:
        assert cli.main(["build", str(app), "-q"]) == 0
        out = app / "dist" / "app-win_amd64"
        site = out / "runtime" / "Lib" / "site-packages"
        for name in ("app", "core", "util", "tools", "extraw", "idna"):
            assert (site / name).is_dir(), name
        lock = (app / "pylock.win_amd64.toml").read_text(encoding="utf-8")
        assert 'name = "idna"' in lock
        for local in ("core", "util", "tools"):  # never resolved from an index
            assert f'name = "{local}"' not in lock
        assert 'path = "../../wheels/extraw-1.0-py3-none-any.whl"' in lock
        assert "file://" not in lock
        if wine_available():
            run = run_exe(out / "app.exe")
            assert run.returncode == 0, run.stderr
            assert "MONO core tools util extraw" in run.stdout

        # the lock is portable: the same monorepo checked out elsewhere
        moved = tmp_path / "elsewhere" / "repo"
        shutil.copytree(tmp_path / "repo", moved,
                        ignore=shutil.ignore_patterns("dist"))
        assert cli.main(["build", str(moved / "packages" / "app"), "-q", "--frozen"]) == 0
    finally:
        if old is None:
            os.environ.pop("PYXBUILD_CACHE_DIR", None)
        else:
            os.environ["PYXBUILD_CACHE_DIR"] = old


def test_find_links_written_with_hashes_by_newer_uv_are_localized(tmp_path):
    # uv 0.13 records find-links files with their sha256 already filled in
    (tmp_path / "wheels").mkdir()
    whl = tmp_path / "wheels" / "x-1-py3-none-any.whl"
    whl.write_bytes(b"wheel")
    text = ('lock-version = "1.0"\n[[packages]]\nname = "x"\nwheels = [{ url = "'
            + whl.as_uri() + '", hashes = { sha256 = "' + "0" * 64 + '" } }]\n')
    out = _localize_files(text, tmp_path)
    assert 'path = "wheels/x-1-py3-none-any.whl"' in out and "file://" not in out
    import hashlib
    assert hashlib.sha256(b"wheel").hexdigest() in out  # recomputed from the file
