"""Level 3 of the golden set: dependencies that only exist as sdists.

A local ``find-links`` directory stands in for an index so the test does not
depend on which PyPI projects happen to lack wheels today.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from conftest import run_exe, wine_available
from pyxbuild import cli
from pyxbuild.lock import uv_executable

pytestmark = [pytest.mark.slow, pytest.mark.network]


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip())
    return root


def _sdist(src: Path, links: Path) -> None:
    subprocess.run([uv_executable(), "build", "--quiet", "--sdist", "-o", str(links), str(src)],
                   check=True)


@pytest.fixture(scope="module")
def links(tmp_path_factory):
    base = tmp_path_factory.mktemp("sdists")
    links = base / "links"
    _sdist(_write(base / "pure", {
        "pyproject.toml": """
            [build-system]
            requires = ["hatchling"]
            build-backend = "hatchling.build"
            [project]
            name = "pyxb-test-purepkg"
            version = "1.0"
            [tool.hatch.build.targets.wheel]
            packages = ["purepkg"]
        """,
        "purepkg/__init__.py": "VALUE = 42\n",
    }), links)
    _sdist(_write(base / "cext", {
        "pyproject.toml": """
            [build-system]
            requires = ["setuptools>=68"]
            build-backend = "setuptools.build_meta"
            [project]
            name = "pyxb-test-cext"
            version = "1.0"
        """,
        "setup.py": """
            from setuptools import Extension, setup
            setup(ext_modules=[Extension("cext._speedups", ["cext/_speedups.c"])],
                  packages=["cext"])
        """,
        "cext/__init__.py": "",
        "cext/_speedups.c": """
            #include <Python.h>
            static struct PyModuleDef m = {PyModuleDef_HEAD_INIT, "_speedups"};
            PyMODINIT_FUNC PyInit__speedups(void) { return PyModule_Create(&m); }
        """,
    }), links)
    return links


def _project(tmp_path: Path, dep: str, links: Path, allow_sdist: bool) -> Path:
    return _write(tmp_path / "app", {
        "pyproject.toml": f"""
            [build-system]
            requires = ["hatchling"]
            build-backend = "hatchling.build"
            [project]
            name = "sdist-consumer"
            version = "1.0"
            dependencies = ["{dep}"]
            [project.scripts]
            consumer = "consumer:main"
            [tool.hatch.build.targets.wheel]
            packages = ["consumer"]
            [tool.pyxbuild]
            python = "3.12"
            find-links = ["{links.as_posix()}"]
            allow-sdist = {str(allow_sdist).lower()}
        """,
        "consumer/__init__.py": """
            def main():
                import purepkg
                print("VALUE", purepkg.VALUE)
        """,
    })


def test_pure_sdist_refused_without_opt_in(tmp_path, links, shared_cache, monkeypatch, capsys):
    monkeypatch.setenv("PYXBUILD_CACHE_DIR", str(shared_cache))
    proj = _project(tmp_path, "pyxb-test-purepkg", links, allow_sdist=False)
    assert cli.main(["build", str(proj), "-q"]) == 1
    err = capsys.readouterr().err
    assert "pyxb-test-purepkg" in err
    assert "allow-sdist" in err


def test_pure_sdist_built_on_host_with_opt_in(tmp_path, links, shared_cache, monkeypatch):
    monkeypatch.setenv("PYXBUILD_CACHE_DIR", str(shared_cache))
    proj = _project(tmp_path, "pyxb-test-purepkg", links, allow_sdist=True)
    assert cli.main(["build", str(proj), "-q"]) == 0
    app = proj / "dist" / "sdist-consumer-win_amd64"
    site = app / "runtime" / "Lib" / "site-packages"
    assert (site / "purepkg" / "__init__.py").exists()
    if wine_available():
        proc = run_exe(app / "consumer.exe")
        assert proc.returncode == 0, proc.stderr
        assert "VALUE 42" in proc.stdout


def test_sdist_with_c_code_is_refused(tmp_path, links, shared_cache, monkeypatch, capsys):
    monkeypatch.setenv("PYXBUILD_CACHE_DIR", str(shared_cache))
    proj = _project(tmp_path, "pyxb-test-cext", links, allow_sdist=True)
    assert cli.main(["build", str(proj), "-q"]) == 1
    err = capsys.readouterr().err
    assert "pyxb-test-cext" in err
    assert "platform-specific" in err or "failed" in err
    assert not (proj / "dist" / "sdist-consumer-win_amd64").exists()
