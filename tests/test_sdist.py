"""Level 3 of the golden set: dependencies that only exist as sdists.

A local ``find-links`` directory stands in for an index so the test does not
depend on which PyPI projects happen to lack wheels today.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from conftest import run_exe, wine_available
from pyxbuild import cli, pe
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
            static PyObject *add(PyObject *self, PyObject *args) {
                long a, b;
                if (!PyArg_ParseTuple(args, "ll", &a, &b)) return NULL;
                return PyLong_FromLong(a + b);
            }
            static PyObject *long_size(PyObject *self, PyObject *unused) {
                return PyLong_FromSize_t(sizeof(long));  /* 4 on Windows (LLP64) */
            }
            static PyMethodDef methods[] = {
                {"add", add, METH_VARARGS, NULL},
                {"long_size", long_size, METH_NOARGS, NULL},
                {NULL, NULL, 0, NULL}};
            static struct PyModuleDef m = {PyModuleDef_HEAD_INIT, "_speedups", NULL, -1,
                                           methods};
            PyMODINIT_FUNC PyInit__speedups(void) { return PyModule_Create(&m); }
        """,
    }), links)
    return links


def _project(tmp_path: Path, dep: str, links: Path, allow_sdist: bool,
             extra: str = "", main: str = "import purepkg\n    print('VALUE', purepkg.VALUE)"
             ) -> Path:
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
            {extra}
        """,
        "consumer/__init__.py": "def main():\n    " + main + "\n",
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


@pytest.mark.skipif(not (shutil.which("zig") or importlib.util.find_spec("ziglang")),
                    reason="needs zig (pip install ziglang)")
def test_sdist_with_c_code_cross_compiled_with_zig(tmp_path, links, shared_cache, monkeypatch):
    """Experimental cross-compilation: the C extension is built on this host for Windows."""
    monkeypatch.setenv("PYXBUILD_CACHE_DIR", str(shared_cache))
    proj = _project(tmp_path, "pyxb-test-cext", links, allow_sdist=False,
                    extra='cross-compile = ["pyxb-test-cext"]',
                    main="from cext import _speedups as s\n    "
                         "print('ADD', s.add(40, 2), 'LONG', s.long_size())")
    assert cli.main(["build", str(proj), "-q"]) == 0
    app = proj / "dist" / "sdist-consumer-win_amd64"
    site = app / "runtime" / "Lib" / "site-packages"
    (pyd,) = site.glob("cext/_speedups.cp312-win_amd64.pyd")
    info = pe.parse(pyd.read_bytes())
    assert info.machine == pe.IMAGE_FILE_MACHINE_AMD64
    assert "python312.dll" in [i.lower() for i in info.imports]
    assert not list(site.glob("cext/*.lib")) and not list(site.glob("cext/*.pdb"))
    if wine_available():
        proc = run_exe(app / "consumer.exe")
        assert proc.returncode == 0, proc.stderr
        assert "ADD 42 LONG 4" in proc.stdout


def test_pure_python_fallback_for_real_packages(tmp_path, shared_cache, monkeypatch):
    """msgpack and PyYAML built from their PyPI sdists without the C extension."""
    monkeypatch.setenv("PYXBUILD_CACHE_DIR", str(shared_cache))
    proj = _write(tmp_path / "app", {
        "pyproject.toml": """
            [build-system]
            requires = ["hatchling"]
            build-backend = "hatchling.build"
            [project]
            name = "pure-consumer"
            version = "1.0"
            dependencies = ["msgpack", "pyyaml"]
            [project.scripts]
            consumer = "consumer:main"
            [tool.hatch.build.targets.wheel]
            packages = ["consumer"]
            [tool.pyxbuild]
            python = "3.12"
            pure-python = ["msgpack", "PyYAML"]
        """,
        "consumer/__init__.py": """
            def main():
                import msgpack, yaml
                assert msgpack.unpackb(msgpack.packb({"a": [1, 2]})) == {"a": [1, 2]}
                assert not yaml.__with_libyaml__
                print("PURE", msgpack.Packer.__module__, yaml.safe_load("x: 1"))
        """,
    })
    assert cli.main(["build", str(proj), "-q"]) == 0
    lock = (proj / "pylock.win_amd64.toml").read_text()
    assert "msgpack" in lock and "sdist" in lock
    site = proj / "dist" / "pure-consumer-win_amd64" / "runtime" / "Lib" / "site-packages"
    assert not list(site.glob("msgpack/*.pyd")) and not list(site.glob("yaml/*.pyd"))
    if wine_available():
        proc = run_exe(site.parents[2] / "consumer.exe")
        assert proc.returncode == 0, proc.stderr
        assert "PURE msgpack.fallback {'x': 1}" in proc.stdout
