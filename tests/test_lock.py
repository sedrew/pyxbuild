import textwrap
import tomllib

import pytest

from pyxbuild.errors import ResolveError
from pyxbuild.lock import LockInputs, _annotate, read_lock, requirement_lines, select_files
from pyxbuild.targets import WIN_AMD64

SHA = "0" * 64


def wheel(name):
    return f'{{ url = "https://example.invalid/{name}", hashes = {{ sha256 = "{SHA}" }} }}'


LOCK = textwrap.dedent(f"""
    lock-version = "1.0"
    created-by = "test"

    [[packages]]
    name = "numpy"
    version = "2.5.3"
    wheels = [
        {wheel("numpy-2.5.3-cp312-cp312-manylinux_2_17_x86_64.whl")},
        {wheel("numpy-2.5.3-cp312-cp312-macosx_14_0_arm64.whl")},
        {wheel("numpy-2.5.3-cp312-cp312-win_amd64.whl")},
        {wheel("numpy-2.5.3-cp313-cp313-win_amd64.whl")},
    ]

    [[packages]]
    name = "charset-normalizer"
    version = "3.5.2"
    wheels = [
        {wheel("charset_normalizer-3.5.2-py3-none-any.whl")},
        {wheel("charset_normalizer-3.5.2-cp37-abi3-win_amd64.whl")},
    ]

    [[packages]]
    name = "appnope"
    version = "0.1.4"
    marker = "sys_platform == 'darwin'"
    wheels = [{wheel("appnope-0.1.4-py2.py3-none-any.whl")}]

    [[packages]]
    name = "pywin32"
    version = "312"
    marker = "sys_platform == 'win32'"
    wheels = [{wheel("pywin32-312-cp312-cp312-win_amd64.whl")}]
""")


@pytest.fixture
def lock(tmp_path):
    p = tmp_path / "pylock.toml"
    p.write_text(LOCK)
    return read_lock(p)


def env(v="3.12.13"):
    return WIN_AMD64.marker_environment(v)


def test_select_prefers_specific_then_abi3_and_honours_markers(lock):
    sel = select_files(lock, WIN_AMD64, env(), WIN_AMD64.tags("3.12"))
    chosen = {s.package.name: s.file.name for s in sel}
    assert chosen == {
        "charset-normalizer": "charset_normalizer-3.5.2-cp37-abi3-win_amd64.whl",
        "numpy": "numpy-2.5.3-cp312-cp312-win_amd64.whl",
        "pywin32": "pywin32-312-cp312-cp312-win_amd64.whl",
    }


def test_missing_wheel_for_python_version_is_reported(lock):
    with pytest.raises(ResolveError) as exc:
        select_files(lock, WIN_AMD64, env("3.14.6"), WIN_AMD64.tags("3.14"))
    msg = str(exc.value)
    assert "numpy" in msg and "pywin32" in msg and "cp314" in msg


def test_sdist_only_requires_opt_in(tmp_path):
    p = tmp_path / "pylock.toml"
    p.write_text(textwrap.dedent(f"""
        lock-version = "1.0"
        [[packages]]
        name = "eel"
        version = "0.18.1"
        sdist = {wheel("eel-0.18.1.tar.gz")}
    """))
    lock = read_lock(p)
    with pytest.raises(ResolveError, match="only an sdist"):
        select_files(lock, WIN_AMD64, env(), WIN_AMD64.tags("3.12"))
    sel = select_files(lock, WIN_AMD64, env(), WIN_AMD64.tags("3.12"), allow_sdist=True)
    assert sel[0].is_wheel is False


def test_missing_hash_is_an_error(tmp_path):
    p = tmp_path / "pylock.toml"
    p.write_text('lock-version = "1.0"\n[[packages]]\nname = "x"\n'
                 'wheels = [{ url = "https://e/x-1-py3-none-any.whl" }]\n')
    with pytest.raises(ResolveError, match="no sha256"):
        read_lock(p)


def test_unsupported_lock_version(tmp_path):
    p = tmp_path / "pylock.toml"
    p.write_text('lock-version = "2.0"\n')
    with pytest.raises(ResolveError, match="lock-version"):
        read_lock(p)


def test_requirement_lines_evaluate_markers_for_target():
    lines = requirement_lines(
        [
            "requests>=2.32",
            'appnope; sys_platform == "darwin"',
            'pywin32>=306; sys_platform == "win32"',
            'colorama; os_name == "nt"',
            'pytest; extra == "test"',
            'rich; extra == "pretty"',
            'uvloop; platform_system != "Windows"',
        ],
        WIN_AMD64, env(), extras=["pretty"],
    )
    assert lines == ["colorama", "pywin32>=306", "requests>=2.32", "rich"]


def test_annotated_lock_is_valid_toml_and_records_inputs():
    inputs = LockInputs(("requests>=2.32",), "win_amd64", "3.12.13", None, (), False)
    raw = '# header\nlock-version = "1.0"\ncreated-by = "uv"\n\n[[packages]]\nname = "x"\n'
    data = tomllib.loads(_annotate(raw, inputs, WIN_AMD64, "3.12.13"))
    assert data["lock-version"] == "1.0"
    assert data["environments"] == [
        "sys_platform == 'win32' and platform_machine == 'AMD64' and "
        "python_full_version == '3.12.13'"
    ]
    assert data["tool"]["pyxbuild"]["inputs-sha256"] == inputs.digest()
    assert data["packages"][0]["name"] == "x"


def test_inputs_digest_changes_with_inputs():
    a = LockInputs(("a",), "win_amd64", "3.12.13", None, (), False)
    b = LockInputs(("a",), "win_amd64", "3.13.14", None, (), False)
    c = LockInputs(("a", "b"), "win_amd64", "3.12.13", None, (), False)
    assert len({a.digest(), b.digest(), c.digest()}) == 3
    assert a.digest() == LockInputs(("a",), "win_amd64", "3.12.13", None, (), False).digest()
