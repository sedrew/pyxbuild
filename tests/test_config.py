import textwrap

import pytest

from pyxbuild.config import EntryPoint, load_config
from pyxbuild.errors import ConfigError


def write(tmp_path, text):
    (tmp_path / "pyproject.toml").write_text(textwrap.dedent(text))
    return tmp_path


BASIC = """
[project]
name = "MyApp"
version = "1.2.0"
requires-python = ">=3.12"
dependencies = ["requests>=2.32"]

[project.scripts]
myapp = "myapp.main:run"

[project.gui-scripts]
myapp-gui = "myapp.gui:App.start"
"""


def test_defaults(tmp_path):
    cfg = load_config(write(tmp_path, BASIC))
    assert cfg.name == "MyApp"
    assert cfg.normalized_name == "myapp"
    assert cfg.targets == ["win_amd64"]
    assert cfg.mode == "folder"
    assert cfg.runtime == "python-build-standalone"
    assert cfg.choose_python() == "3.14"
    assert cfg.lock_path("win_amd64") == tmp_path / "pylock.win_amd64.toml"
    assert [(e.name, e.module, e.attr, e.gui) for e in cfg.entry_points] == [
        ("myapp", "myapp.main", "run", False),
        ("myapp-gui", "myapp.gui", "App.start", True),
    ]


def test_tool_table(tmp_path):
    cfg = load_config(write(tmp_path, BASIC + """
[tool.pyxbuild]
targets = ["win_amd64"]
python = "3.12"
mode = "folder"
lock = "pylock.toml"
exclude = ["tests/**", "*.pyi"]
zip = true
scripts = ["myapp"]
"""))
    assert cfg.choose_python() == "3.12"
    assert cfg.lock_path("win_amd64") == tmp_path / "pylock.toml"
    assert cfg.exclude == ["tests/**", "*.pyi"]
    assert cfg.zip is True
    assert [e.name for e in cfg.entry_points] == ["myapp"]


@pytest.mark.parametrize(
    ("requires", "expected"),
    [(">=3.11", "3.14"), (">=3.11,<3.13", "3.12"), ("~=3.11.0", "3.11"), ("==3.13.*", "3.13")],
)
def test_choose_python_from_requires_python(tmp_path, requires, expected):
    text = BASIC.replace('requires-python = ">=3.12"', f'requires-python = "{requires}"')
    assert load_config(write(tmp_path, text)).choose_python() == expected


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ('python = "3.10"', "unsupported python"),
        ('python = "3.11"', "does not satisfy requires-python"),
        ('targets = ["linux"]', "unknown target"),
        ('mode = "zip"', "mode must be"),
        ('runtime = "embeddable"', "unsupported runtime"),
        ('exclude = "x"', "exclude must be a list"),
        ('colour = "red"', "unknown key"),
        ('scripts = ["nope"]', "scripts not found"),
    ],
)
def test_invalid_tool_values(tmp_path, extra, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, BASIC + "\n[tool.pyxbuild]\n" + extra + "\n"))


def test_missing_entry_points(tmp_path):
    with pytest.raises(ConfigError, match="nothing to build"):
        load_config(write(tmp_path, '[project]\nname = "x"\nversion = "1"\n'))


def test_missing_pyproject(tmp_path):
    with pytest.raises(ConfigError, match="no pyproject.toml"):
        load_config(tmp_path)


@pytest.mark.parametrize("value", ["noattr", "a:b:c", "1abc:run", "a.b:", "a b:run"])
def test_bad_entry_point(value):
    with pytest.raises(ConfigError):
        EntryPoint.parse("x", value, gui=False)


def test_entry_point_with_extras_is_accepted():
    e = EntryPoint.parse("x", "pkg.mod:func [extra1]", gui=False)
    assert (e.module, e.attr) == ("pkg.mod", "func")
