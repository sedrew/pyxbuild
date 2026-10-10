"""Installing for Windows' file system from any host: case-insensitive names,
names Windows cannot store, excludes applied while installing, long paths and
post-install scripts."""

import os

import pytest

from conftest import make_pe, make_wheel
from pyxbuild import doctor
from pyxbuild.errors import PyxbuildError
from pyxbuild.install import Excluder, InstallState, install_wheel, windows_name_problem


@pytest.fixture
def runtime(tmp_path):
    rt = tmp_path / "app" / "runtime"
    (rt / "Lib" / "site-packages").mkdir(parents=True)
    (rt / "python.exe").write_bytes(make_pe(["KERNEL32.dll"], dll=False))
    return rt


def site(rt):
    return rt / "Lib" / "site-packages"


def names(directory):
    return sorted(os.listdir(directory))


def record(rt, dist):
    return next(site(rt).glob(f"{dist}-*.dist-info/RECORD")).read_text()


def test_case_only_directory_clash_merges_like_windows(tmp_path, runtime):
    # pycryptodome ships Crypto/, the unrelated "crypto" package ships crypto/:
    # on Windows (and macOS) that is one folder, on Linux it would be two.
    state = InstallState(runtime)
    install_wheel(make_wheel(tmp_path / "a", "pycryptodome", files={
        "Crypto/__init__.py": "", "Crypto/Cipher.py": "A = 1\n"}), runtime, state)
    install_wheel(make_wheel(tmp_path / "b", "crypto", files={
        "crypto/shell.py": "B = 1\n"}), runtime, state)
    assert [n for n in names(site(runtime)) if n.lower() == "crypto"] == ["Crypto"]
    assert names(site(runtime) / "Crypto") == ["Cipher.py", "__init__.py", "shell.py"]
    assert "crypto/shell.py,sha256=" in record(runtime, "crypto")  # wheel's spelling, as pip
    report = doctor.Report()
    state.add_findings(report)
    [finding] = report.findings
    assert finding.level == doctor.WARNING
    assert "crypto-1.0-py3-none-any.whl" in finding.message
    assert "pycryptodome-1.0-py3-none-any.whl" in finding.message


def test_case_only_file_clash_with_different_content_fails(tmp_path, runtime):
    state = InstallState(runtime)
    install_wheel(make_wheel(tmp_path / "a", "a", files={"Mod/__init__.py": "a = 1\n"}),
                  runtime, state)
    with pytest.raises(PyxbuildError, match=r"Mod/__init__\.py \(installed by a-1\.0"):
        install_wheel(make_wheel(tmp_path / "b", "b", files={"mod/__init__.py": "b = 1\n"}),
                      runtime, state)


def test_case_clash_inside_one_wheel(tmp_path, runtime):
    with pytest.raises(PyxbuildError, match="different content"):
        install_wheel(make_wheel(tmp_path / "a", "a", files={
            "pkg/Readme.txt": "1", "pkg/README.txt": "2"}), runtime)


def test_data_files_use_the_runtime_spelling(tmp_path, runtime):
    state = InstallState(runtime)
    install_wheel(make_wheel(tmp_path / "a", "a", files={
        "a/__init__.py": "", "a-1.0.data/data/lib/site-packages/a_extra.py": ""}),
        runtime, state)
    assert names(runtime).count("Lib") == 1 and "lib" not in names(runtime)
    assert (site(runtime) / "a_extra.py").is_file()
    report = doctor.Report()
    state.add_findings(report)
    assert [f.level for f in report.findings] == [doctor.INFO]  # same as pip on Windows


@pytest.mark.parametrize("bad", ["pkg/aux.py", "pkg/CON", "pkg/nul.txt", "pkg/com1.py",
                                 "pkg/a:b.py", "pkg/what?.txt", "pkg/trailing./x.py",
                                 "pkg/space /x.py"])
def test_names_windows_cannot_store_are_refused(tmp_path, runtime, bad):
    with pytest.raises(PyxbuildError, match="for Windows") as exc:
        install_wheel(make_wheel(tmp_path / "a", "a", files={"pkg/__init__.py": "", bad: "x"}),
                      runtime)
    assert "exclude" in (exc.value.hint or "")


def test_drive_letters_are_still_refused_as_outside(tmp_path, runtime):
    with pytest.raises(PyxbuildError, match="outside the install folder"):
        install_wheel(make_wheel(tmp_path / "a", "a", files={"C:/evil.py": ""}), runtime)


def test_windows_name_problem_allows_ordinary_names():
    for ok in ["console.py", "lpt0.py", "com10", "aux_utils.py", "con_test.py", "a.b.c", "π.txt"]:
        assert windows_name_problem(ok) is None, ok


def test_excluded_files_are_not_written(tmp_path, runtime):
    state = InstallState(runtime, Excluder(["pkg/aux.py", "tests/"]))
    install_wheel(make_wheel(tmp_path / "a", "a", files={
        "pkg/__init__.py": "", "pkg/aux.py": "x", "tests/test_a.py": ""}), runtime, state)
    assert not (site(runtime) / "pkg" / "aux.py").exists()
    assert not (site(runtime) / "tests").exists()
    rec = record(runtime, "a")
    assert "pkg/aux.py" not in rec and "tests/" not in rec and "pkg/__init__.py" in rec
    assert sorted(state.excluded) == ["pkg/aux.py", "tests/test_a.py"]


def _bundle_with_path(runtime, length):
    """Put a file whose path relative to the app folder has ``length`` characters."""
    prefix = "runtime/Lib/site-packages/"
    name = "f" * (length - len(prefix) - len("deep/") - 3) + ".py"
    target = site(runtime) / "deep" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("")


@pytest.mark.parametrize(("length", "level"), [
    (150, None), (230, doctor.WARNING), (258, doctor.ERROR)])
def test_long_paths_report_the_folder_budget(runtime, length, level):
    _bundle_with_path(runtime, length)
    report = doctor.check_bundle(runtime.parent, runtime)
    found = [f for f in report.findings if "MAX_PATH" in f.message]
    if level is None:
        assert not found
    else:
        [f] = found
        assert f.level == level
        if level == doctor.WARNING:
            assert f"at most {259 - length} characters" in f.message


def test_long_directory_counts_with_room_for_a_file_name(runtime):
    # CreateDirectoryW allows MAX_PATH - 12: a deep folder with a short file name
    deep = site(runtime) / ("d" * 200)
    deep.mkdir()
    (deep / "a.py").write_text("")
    [f] = [f for f in doctor.check_bundle(runtime.parent, runtime).findings
           if "MAX_PATH" in f.message]
    rel_dir = len("runtime/Lib/site-packages/") + 200
    assert f"needs {rel_dir + 12} characters" in f.message


def test_post_install_scripts_are_reported(tmp_path, runtime):
    install_wheel(make_wheel(tmp_path / "w", "pywin32", files={
        "win32/__init__.py": "", "pywin32-1.0.data/scripts/pywin32_postinstall.py": "#"}),
        runtime)
    report = doctor.check_bundle(runtime.parent, runtime)
    assert any("pywin32_postinstall.py is a post-install script" in f.message
               for f in report.findings)
