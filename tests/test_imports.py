import textwrap

import pytest

from conftest import make_wheel
from pyxbuild import doctor
from pyxbuild.imports import available_modules, check_imports, scan_source


@pytest.fixture
def runtime(tmp_path):
    rt = tmp_path / "runtime"
    site = rt / "Lib" / "site-packages"
    (site / "requests").mkdir(parents=True)
    (site / "requests" / "__init__.py").write_text("")
    (site / "requests-2.0.dist-info").mkdir()
    (site / "six.py").write_text("")
    (site / "nspkg" / "sub").mkdir(parents=True)  # namespace package
    (rt / "Lib" / "json").mkdir()
    (rt / "Lib" / "importlib").mkdir()
    (rt / "Lib" / "os.py").write_text("")
    (rt / "DLLs").mkdir()
    (rt / "DLLs" / "_ssl.pyd").write_bytes(b"")
    return rt


def project(tmp_path, code):
    return make_wheel(tmp_path / "w", "app", files={
        "app/__init__.py": "",
        "app/main.py": textwrap.dedent(code),
    })


def findings(runtime, wheel):
    report = doctor.Report()
    check_imports(runtime, wheel, report)
    return {(f.level, f.message.split("'")[1], f.path) for f in report.findings}


def test_available_modules(runtime):
    names = available_modules(runtime)
    assert {"requests", "six", "nspkg", "json", "os", "_ssl", "winreg", "sys"} <= names
    assert "requests-2" not in names and "fcntl" not in names and "pwd" not in names


def test_reports_undeclared_and_posix_only(tmp_path, runtime):
    wheel = project(tmp_path, """
        import os, json, sys
        import requests
        from app import helpers
        from . import local
        import yaml
        import fcntl
        from nspkg.sub import thing

        try:
            import ujson as fastjson
        except ImportError:
            fastjson = None

        if sys.platform != "win32":
            import termios

        def lazy():
            import numpy
            return importlib.import_module("toml_lib")

        import importlib
        plugin = importlib.import_module("missing_plugin")
    """)
    got = findings(runtime, wheel)
    assert got == {
        ("error", "yaml", "app/main.py:6"),
        ("error", "fcntl", "app/main.py:7"),
        ("warning", "ujson", "app/main.py:11"),
        ("warning", "numpy", "app/main.py:19"),
        ("warning", "toml_lib", "app/main.py:20"),
        ("error", "missing_plugin", "app/main.py:23"),
    }


def test_hint_names_distribution(tmp_path, runtime):
    report = doctor.Report()
    check_imports(runtime, project(tmp_path, "import yaml\nfrom PIL import Image\n"), report)
    text = report.format()
    assert "'PyYAML'" in text and "'Pillow'" in text


def test_type_checking_imports_are_soft():
    uses = scan_source("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
                       "    import pandas\n", "x.py")
    (pandas,) = [u for u in uses if u.module == "pandas"]
    assert pandas.guarded


def test_pth_directories_count_as_available(runtime):
    site = runtime / "Lib" / "site-packages"
    (site / "win32" / "lib").mkdir(parents=True)
    (site / "win32" / "win32api.pyd").write_bytes(b"")
    (site / "win32" / "lib" / "pywintypes.py").write_text("")
    (site / "pywin32.pth").write_text("# comment\nwin32\nwin32\\lib\nimport pywin32_bootstrap\n")
    assert {"win32api", "pywintypes"} <= available_modules(runtime)
