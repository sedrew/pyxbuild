"""Regression tests for defects found in the code review (October 2026)."""

import struct
import zipfile

import pytest

from conftest import make_wheel
from pyxbuild.download import fetch
from pyxbuild.errors import ConfigError, DownloadError, PyxbuildError
from pyxbuild.install import install_wheel
from pyxbuild.lock import _localize_files, read_lock, requirement_lines
from pyxbuild.resources import read_ico
from pyxbuild.targets import WIN_AMD64


def test_wheel_entry_outside_install_folder_is_refused(tmp_path):
    rt = tmp_path / "rt"
    (rt / "Lib" / "site-packages").mkdir(parents=True)
    whl = make_wheel(tmp_path / "w", "evil")
    with zipfile.ZipFile(whl, "a") as zf:
        zf.writestr("../../../pwned.txt", "x")
    with pytest.raises(PyxbuildError):
        install_wheel(whl, rt)
    assert not (tmp_path / "pwned.txt").exists()


def test_bad_console_script_name_is_refused(tmp_path):
    rt = tmp_path / "rt"
    (rt / "Lib" / "site-packages").mkdir(parents=True)
    whl = make_wheel(tmp_path / "w", "evil",
                     entry_points="[console_scripts]\n..\\\\..\\\\x = evil:main\n")
    with pytest.raises(PyxbuildError):
        install_wheel(whl, rt)


@pytest.mark.parametrize("name", ["../x.whl", "a/../../b.whl", "a\\b.whl", "C:x.whl", "..", ""])
def test_unsafe_cache_file_names(cache_dir, name):
    with pytest.raises(DownloadError):
        fetch("https://example.invalid/x", "0" * 64, name)


def test_missing_local_file_is_a_download_error(cache_dir, tmp_path):
    with pytest.raises(DownloadError):
        fetch(str(tmp_path / "missing.whl"), "0" * 64, "missing.whl")


def test_target_tags_never_use_host_abi_flags(monkeypatch):
    import sysconfig

    real = sysconfig.get_config_var
    monkeypatch.setattr(sysconfig, "get_config_var",
                        lambda k: 1 if k in ("Py_GIL_DISABLED", "Py_DEBUG") else real(k))
    abis = {t.abi for t in WIN_AMD64.tags("3.13")}
    assert abis == {"cp313", "abi3", "none"}


def test_self_referencing_extras_are_expanded():
    env = WIN_AMD64.marker_environment("3.12.13")
    lines = requirement_lines(
        ["requests", 'pyside6; extra == "gui"', 'click; extra == "cli"',
         'myapp[gui,cli]; extra == "all"'],
        WIN_AMD64, env, extras=["all"], project="MyApp")
    assert lines == ["click", "pyside6", "requests"]


def test_local_find_links_files_become_relative_and_hashed(tmp_path):
    (tmp_path / "wheels").mkdir()
    (tmp_path / "wheels" / "x-1-py3-none-any.whl").write_bytes(b"wheel")
    text = ('lock-version = "1.0"\n[[packages]]\nname = "x"\nwheels = [{ url = "file://'
            + str(tmp_path) + '/wheels/x-1-py3-none-any.whl", hashes = {} }]\n')
    out = _localize_files(text, tmp_path)
    assert 'path = "wheels/x-1-py3-none-any.whl"' in out
    (tmp_path / "pylock.toml").write_text(out)
    (pkg,) = read_lock(tmp_path / "pylock.toml").packages
    assert pkg.wheels[0].url == "wheels/x-1-py3-none-any.whl"
    assert len(pkg.wheels[0].sha256) == 64


def test_truncated_icon(tmp_path):
    ico = tmp_path / "a.ico"
    ico.write_bytes(struct.pack("<HHH", 0, 1, 5))
    with pytest.raises(ConfigError, match="truncated"):
        read_ico(ico)
