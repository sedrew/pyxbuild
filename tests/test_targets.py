import json

import pytest
from packaging.markers import Marker
from packaging.tags import Tag

from pyxbuild.errors import ConfigError
from pyxbuild.targets import WIN_AMD64, get_target, markers_from_build_details


@pytest.fixture
def env():
    return WIN_AMD64.marker_environment("3.12.13")


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ('sys_platform == "win32"', True),
        ('sys_platform == "darwin"', False),  # appnope must not leak in from a Mac host
        ('sys_platform == "linux"', False),
        ('os_name == "nt"', True),
        ('platform_system == "Windows"', True),
        ('platform_machine == "AMD64"', True),
        ('platform_machine == "x86_64"', False),
        ('platform_machine == "arm64"', False),
        ('python_version >= "3.12"', True),
        ('python_version < "3.12"', False),
        ('python_full_version == "3.12.13"', True),
        ('implementation_name == "cpython"', True),
        ('platform_python_implementation == "PyPy"', False),
        ('sys_platform == "win32" and python_version < "3.13"', True),
    ],
)
def test_marker_evaluation_uses_target_not_host(env, marker, expected):
    assert Marker(marker).evaluate(env) is expected
    assert WIN_AMD64.evaluate(marker, env) is expected


def test_extra_markers_are_false_without_extras(env):
    assert WIN_AMD64.evaluate('extra == "dev"', env) is False
    assert WIN_AMD64.evaluate(None, env) is True
    assert WIN_AMD64.evaluate("", env) is True


def test_tag_priority():
    tags = WIN_AMD64.tags("3.12")
    rank = {t: i for i, t in enumerate(tags)}
    specific = Tag("cp312", "cp312", "win_amd64")
    abi3 = Tag("cp312", "abi3", "win_amd64")
    older_abi3 = Tag("cp38", "abi3", "win_amd64")
    pure = Tag("py3", "none", "any")
    assert rank[specific] < rank[abi3] < rank[older_abi3] < rank[pure]


@pytest.mark.parametrize(
    "tag",
    [
        Tag("cp312", "cp312", "manylinux_2_17_x86_64"),
        Tag("cp312", "cp312", "macosx_11_0_arm64"),
        Tag("cp312", "cp312", "win32"),
        Tag("cp312", "cp312", "win_arm64"),
        Tag("cp313", "cp313", "win_amd64"),
        Tag("cp312", "cp312t", "win_amd64"),
        Tag("pp310", "pypy310_pp73", "win_amd64"),
    ],
)
def test_foreign_tags_rejected(tag):
    assert tag not in set(WIN_AMD64.tags("3.12"))


def test_abi3_from_newer_python_rejected():
    assert Tag("cp313", "abi3", "win_amd64") not in set(WIN_AMD64.tags("3.12"))


def test_get_target():
    assert get_target("win_amd64") is WIN_AMD64
    with pytest.raises(ConfigError, match="unknown target"):
        get_target("win_arm64")


def test_build_details_overrides(tmp_path):
    p = tmp_path / "build-details.json"
    p.write_text(json.dumps({
        "schema_version": "1.0", "platform": "win-amd64",
        "language": {"version": "3.14"}, "implementation": {"name": "cpython"},
    }))
    overrides = markers_from_build_details(p)
    env = WIN_AMD64.marker_environment("3.14.6", overrides)
    assert env["python_version"] == "3.14"
    assert env["sys_platform"] == "win32"
    assert env["platform_machine"] == "AMD64"
