import csv
import io

import pytest

from conftest import make_pe, make_wheel
from pyxbuild import doctor
from pyxbuild.install import Excluder, apply_excludes, install_wheel
from pyxbuild.launcher import decode_manifest


@pytest.fixture
def runtime(tmp_path):
    rt = tmp_path / "app" / "runtime"
    (rt / "Lib" / "site-packages").mkdir(parents=True)
    (rt / "python.exe").write_bytes(make_pe(["python312.dll", "KERNEL32.dll"], dll=False))
    (rt / "python312.dll").write_bytes(make_pe(["KERNEL32.dll", "VCRUNTIME140.dll"]))
    (rt / "vcruntime140.dll").write_bytes(make_pe(["KERNEL32.dll"]))
    return rt


def record_rows(site, dist):
    text = next(site.glob(f"{dist}-*.dist-info/RECORD")).read_text()
    return {row[0]: row for row in csv.reader(io.StringIO(text)) if row}


def test_install_layout_record_and_installer(tmp_path, runtime):
    whl = make_wheel(
        tmp_path / "w", "demo-pkg", "2.0",
        files={"demo_pkg/__init__.py": "X = 1\n", "demo.pth": "import os\n",
               "demo_pkg-2.0.data/data/share/demo.txt": "data\n",
               "demo_pkg-2.0.data/scripts/tool.cmd": "@echo off\n"},
        entry_points=("[console_scripts]\ndemo = demo_pkg:main\n"
                      "[gui_scripts]\ndemow = demo_pkg:gui\n"),
    )
    install_wheel(whl, runtime)
    site = runtime / "Lib" / "site-packages"
    assert (site / "demo_pkg" / "__init__.py").read_text() == "X = 1\n"
    assert (site / "demo.pth").exists()
    assert (runtime / "share" / "demo.txt").exists()
    assert (runtime / "Scripts" / "tool.cmd").exists()
    info = site / "demo_pkg-2.0.dist-info"
    assert (info / "INSTALLER").read_text() == "pyxbuild"
    rows = record_rows(site, "demo_pkg")
    # paths outside site-packages are relative POSIX paths on every host OS
    assert "../../Scripts/demo.exe" in rows
    assert "../../share/demo.txt" in rows
    assert "demo_pkg-2.0.dist-info/RECORD" in rows
    assert all("\\" not in p and not p.startswith("/") for p in rows)
    # console scripts get pyxbuild launchers that find ..\python.exe
    fields = decode_manifest((runtime / "Scripts" / "demo.exe").read_bytes())
    assert fields["python"] == "..\\python.exe"
    assert fields["entry-point"] == "demo_pkg:main"
    assert decode_manifest((runtime / "Scripts" / "demow.exe").read_bytes())["python"] == (
        "..\\pythonw.exe")


def test_install_conflicting_file_fails(tmp_path, runtime):
    from pyxbuild.errors import PyxbuildError

    install_wheel(make_wheel(tmp_path / "a", "a", files={"shared/x.py": ""}), runtime)
    with pytest.raises(PyxbuildError, match="overwrites"):
        install_wheel(make_wheel(tmp_path / "b", "b", files={"shared/x.py": ""}), runtime)


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("*.pyi", "pkg/sub/mod.pyi", True),
        ("*.pyi", "pkg/sub/mod.py", False),
        ("tests/", "pkg/tests/test_x.py", True),
        ("tests", "pkg/tests/test_x.py", True),
        ("tests/**", "tests/test_x.py", True),
        ("tests/**", "pkg/tests/test_x.py", False),
        ("**/tests/**", "pkg/tests/test_x.py", True),
        ("pkg/*.txt", "pkg/a.txt", True),
        ("pkg/*.txt", "pkg/sub/a.txt", False),
        ("pkg/**/*.txt", "pkg/sub/deep/a.txt", True),
        ("data[0-9].bin", "x/data5.bin", True),
        ("data[!0-9].bin", "x/data5.bin", False),
    ],
)
def test_exclude_patterns(pattern, path, expected):
    assert Excluder([pattern]).matches(path) is expected


def test_apply_excludes_updates_record(tmp_path, runtime):
    whl = make_wheel(tmp_path / "w", "pkg", files={
        "pkg/__init__.py": "", "pkg/tests/test_a.py": "", "pkg/stub.pyi": ""})
    install_wheel(whl, runtime)
    site = runtime / "Lib" / "site-packages"
    removed = apply_excludes(site, Excluder(["tests/", "*.pyi"]))
    assert sorted(removed) == ["pkg/stub.pyi", "pkg/tests/test_a.py"]
    assert not (site / "pkg" / "tests").exists()
    rows = record_rows(site, "pkg")
    assert "pkg/stub.pyi" not in rows and "pkg/tests/test_a.py" not in rows
    assert "pkg/__init__.py" in rows


def levels(report):
    return {(f.level, f.path, f.message.split(" ")[0]) for f in report.findings}


def test_doctor_clean_bundle(runtime):
    report = doctor.check_bundle(runtime.parent, runtime)
    assert report.findings == []


def test_doctor_reports_missing_and_vc_runtime(runtime):
    site = runtime / "Lib" / "site-packages"
    (site / "fast").mkdir()
    (site / "fast" / "_ext.pyd").write_bytes(make_pe(
        ["python312.dll", "KERNEL32.dll", "MSVCP140.dll", "libmissing.dll", "libhere.dll",
         "api-ms-win-crt-runtime-l1-1-0.dll"]))
    (site / "fast" / "libhere.dll").write_bytes(make_pe(["KERNEL32.dll"]))
    (site / "fast.libs").mkdir()
    (site / "fast" / "_other.pyd").write_bytes(make_pe(["libvendored.dll"]))
    (site / "fast.libs" / "libvendored.dll").write_bytes(make_pe(["KERNEL32.dll"]))
    report = doctor.check_bundle(runtime.parent, runtime)
    msgs = {(f.level, f.message.split(" ")[0], f.message) for f in report.findings}
    errors = [m for lvl, _, m in msgs if lvl == doctor.ERROR]
    assert any("MSVCP140.dll" in m and "Visual C++" in m for m in errors)
    assert any("libmissing.dll" in m for m in errors)
    assert not any("libhere.dll" in m or "api-ms-win" in m or "python312" in m for m in errors)
    infos = [m for lvl, _, m in msgs if lvl == doctor.INFO]
    assert any("libvendored.dll" in m and "add_dll_directory" in m for m in infos)


def test_doctor_wrong_architecture(runtime):
    site = runtime / "Lib" / "site-packages"
    (site / "x.pyd").write_bytes(make_pe(["KERNEL32.dll"], machine=0x14C))
    (site / "t32.exe").write_bytes(make_pe(["KERNEL32.dll"], machine=0x14C, dll=False))
    report = doctor.check_bundle(runtime.parent, runtime)
    by_path = {f.path.rsplit("/", 1)[-1]: f.level for f in report.findings}
    assert by_path == {"x.pyd": doctor.ERROR, "t32.exe": doctor.INFO}


def test_doctor_long_paths_and_known_issues(tmp_path, runtime):
    site = runtime / "Lib" / "site-packages"
    deep = site / ("d" * 120) / ("e" * 60)
    deep.mkdir(parents=True)
    (deep / "f.py").write_text("")
    install_wheel(make_wheel(tmp_path / "w", "playwright"), runtime)
    report = doctor.check_bundle(runtime.parent, runtime)
    assert any("MAX_PATH" in f.message for f in report.findings)
    assert any(f.message.startswith("playwright") for f in report.findings)


def test_copy_runtime_skips_site_packages_dev_files_and_slim(tmp_path):
    from pyxbuild.builder import _copy_runtime

    src = tmp_path / "python"
    for rel in ["python.exe", "include/Python.h", "libs/python312.lib", "Lib/os.py",
                "Lib/__pycache__/os.cpython-312.pyc", "Lib/site-packages/README.txt",
                "Lib/site-packages/pip/__init__.py", "Lib/site-packages/setuptools/x.py",
                "Lib/idlelib/idle.py", "Lib/tkinter/__init__.py", "Lib/turtle.py",
                "tcl/tcl8.6/init.tcl", "DLLs/_tkinter.pyd", "DLLs/tcl86t.dll",
                "DLLs/_testcapi.pyd", "DLLs/_ssl.pyd", "Lib/test/test_x.py"]:
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_bytes(b"x" * 10)
    dest = tmp_path / "out"
    skipped = _copy_runtime(src, dest, ["tests", "idle", "tk"])
    got = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert got == ["DLLs/_ssl.pyd", "Lib/os.py", "Lib/site-packages/README.txt", "python.exe"]
    assert skipped == 10 * 8  # idle, tkinter, turtle, tcl, _tkinter, tcl86t, _testcapi, test


def test_unknown_slim_preset_is_rejected(tmp_path):
    from pyxbuild.config import load_config
    from pyxbuild.errors import ConfigError

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "a"\nversion = "1"\n[project.scripts]\na = "a:b"\n'
        '[tool.pyxbuild]\nslim = ["everything"]\n')
    with pytest.raises(ConfigError, match="unknown slim preset"):
        load_config(tmp_path)


def test_doctor_two_openmp_runtimes(runtime):
    site = runtime / "Lib" / "site-packages"
    (site / "torch" / "lib").mkdir(parents=True)
    (site / "torch" / "lib" / "libiomp5md.dll").write_bytes(make_pe(["KERNEL32.dll"]))
    (site / "sklearn" / ".libs").mkdir(parents=True)
    (site / "sklearn" / ".libs" / "vcomp140.dll").write_bytes(make_pe(["KERNEL32.dll"]))
    report = doctor.check_bundle(runtime.parent, runtime)
    assert any("OMP: Error #15" in f.message for f in report.findings)


def test_doctor_unevaluable_markers(tmp_path, runtime):
    install_wheel(make_wheel(tmp_path / "w", "odd", requires=[
        'winfix; platform_release >= "10"', 'plain']), runtime)
    report = doctor.check_bundle(runtime.parent, runtime)
    assert any("platform_release" in f.message and f.level == doctor.WARNING
               for f in report.findings)
