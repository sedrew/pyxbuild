"""Project configuration: ``[project]`` (PEP 621) and ``[tool.pyxbuild]`` in pyproject.toml."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name

from .errors import ConfigError
from .targets import SUPPORTED_PYTHONS, get_target

RUNTIMES = ("python-build-standalone",)
MODES = ("folder", "onefile")

_ENTRY_RE = re.compile(
    r"^(?P<module>[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)"
    r"\s*:\s*(?P<attr>[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)"
    r"\s*(?:\[[^\]]*\])?$"
)
_SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_KNOWN_KEYS = {
    "targets", "python", "runtime", "mode", "icon", "lock", "exclude", "zip",
    "dist-dir", "index-url", "extra-index-urls", "find-links", "allow-sdist",
    "cross-compile", "pure-python", "extras", "scripts",
    "version-info", "utf8-mode",
}


@dataclass(frozen=True)
class EntryPoint:
    name: str
    module: str
    attr: str
    gui: bool

    @classmethod
    def parse(cls, name: str, value: str, gui: bool) -> EntryPoint:
        if not _SCRIPT_NAME_RE.match(name):
            raise ConfigError(f"invalid script name {name!r}")
        m = _ENTRY_RE.match(value.strip())
        if not m:
            raise ConfigError(
                f"invalid entry point {name} = {value!r}",
                hint="expected 'package.module:function'",
            )
        return cls(name=name, module=m["module"], attr=m["attr"], gui=gui)


@dataclass
class ProjectConfig:
    root: Path
    name: str
    version: str | None
    requires_python: SpecifierSet | None
    entry_points: list[EntryPoint]
    targets: list[str] = field(default_factory=lambda: ["win_amd64"])
    python: str | None = None
    runtime: str = "python-build-standalone"
    mode: str = "folder"
    icon: Path | None = None
    lock: Path | None = None
    exclude: list[str] = field(default_factory=list)
    zip: bool = False
    dist_dir: Path = Path("dist")
    index_url: str | None = None
    extra_index_urls: list[str] = field(default_factory=list)
    find_links: list[str] = field(default_factory=list)
    allow_sdist: bool = False
    pure_python: list[str] = field(default_factory=list)
    """Build these from sdist on the host without their optional C extension."""
    cross_compile: bool | list[str] = False
    """Experimental: compile C extensions of sdists for the target with zig."""
    extras: list[str] = field(default_factory=list)
    version_info: dict[str, str] = field(default_factory=dict)
    utf8_mode: bool = False

    def may_cross_compile(self, package: str) -> bool:
        if isinstance(self.cross_compile, list):
            return canonicalize_name(package) in self.cross_compile
        return self.cross_compile

    @property
    def sdists_allowed(self) -> bool:
        return self.allow_sdist or bool(self.cross_compile) or bool(self.pure_python)

    @property
    def only_sdist(self) -> list[str]:
        """Packages named explicitly for pure-python or cross builds: always from sdist."""
        named = list(self.pure_python)
        if isinstance(self.cross_compile, list):
            named += self.cross_compile
        return sorted(set(named))

    @property
    def normalized_name(self) -> str:
        return canonicalize_name(self.name)

    def lock_path(self, target: str) -> Path:
        if self.lock is not None:
            return self.lock
        return self.root / f"pylock.{target}.toml"

    def choose_python(self) -> str:
        """The X.Y Python version to bundle.

        An explicit ``python`` wins. Otherwise the newest supported version
        that satisfies ``requires-python``.
        """
        if self.python:
            return self.python
        for candidate in reversed(SUPPORTED_PYTHONS):
            if self.requires_python is None or self.requires_python.contains(
                f"{candidate}.0", prereleases=True
            ) or self.requires_python.contains(f"{candidate}.99", prereleases=True):
                return candidate
        raise ConfigError(
            f"requires-python {self.requires_python} excludes every supported Python "
            f"({', '.join(SUPPORTED_PYTHONS)})"
        )


def _str_list(value: Any, key: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"[tool.pyxbuild] {key} must be a list of strings")
    return list(value)


def _str(value: Any, key: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"[tool.pyxbuild] {key} must be a string")
    return value


def load_config(project_dir: Path | str) -> ProjectConfig:
    root = Path(project_dir).resolve()
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        raise ConfigError(f"no pyproject.toml in {root}")
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"cannot parse {pyproject}: {exc}") from None

    project = data.get("project")
    if not isinstance(project, dict):
        raise ConfigError(f"{pyproject} has no [project] table (PEP 621)")
    name = project.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError("[project] name is required")
    version = project.get("version")
    if version is not None and not isinstance(version, str):
        raise ConfigError("[project] version must be a string")
    requires_python = None
    if "requires-python" in project:
        try:
            requires_python = SpecifierSet(project["requires-python"])
        except InvalidSpecifier as exc:
            raise ConfigError(f"invalid requires-python: {exc}") from None

    tool = data.get("tool", {}).get("pyxbuild", {})
    if not isinstance(tool, dict):
        raise ConfigError("[tool.pyxbuild] must be a table")
    unknown = sorted(set(tool) - _KNOWN_KEYS)
    if unknown:
        raise ConfigError(
            f"unknown key(s) in [tool.pyxbuild]: {', '.join(unknown)}",
            hint=f"known keys: {', '.join(sorted(_KNOWN_KEYS))}",
        )

    entry_points: list[EntryPoint] = []
    for table, gui in (("scripts", False), ("gui-scripts", True)):
        for script, value in (project.get(table) or {}).items():
            entry_points.append(EntryPoint.parse(script, value, gui))
    if "scripts" in tool:
        wanted = _str_list(tool["scripts"], "scripts")
        known = {e.name for e in entry_points}
        missing = sorted(set(wanted) - known)
        if missing:
            raise ConfigError(f"[tool.pyxbuild] scripts not found in [project.scripts] or "
                              f"[project.gui-scripts]: {', '.join(missing)}")
        entry_points = [e for e in entry_points if e.name in wanted]
    if not entry_points:
        raise ConfigError(
            "nothing to build: no [project.scripts] or [project.gui-scripts]",
            hint='add e.g. [project.scripts] myapp = "myapp.main:run"',
        )

    cfg = ProjectConfig(
        root=root,
        name=name,
        version=version,
        requires_python=requires_python,
        entry_points=sorted(entry_points, key=lambda e: e.name),
    )
    if "targets" in tool:
        cfg.targets = _str_list(tool["targets"], "targets")
        if not cfg.targets:
            raise ConfigError("[tool.pyxbuild] targets must not be empty")
    for t in cfg.targets:
        get_target(t)
    if "python" in tool:
        cfg.python = _str(tool["python"], "python")
        if cfg.python not in SUPPORTED_PYTHONS:
            raise ConfigError(
                f"unsupported python {cfg.python!r}",
                hint=f"supported: {', '.join(SUPPORTED_PYTHONS)}",
            )
        if requires_python is not None and not (
            requires_python.contains(f"{cfg.python}.0", prereleases=True)
            or requires_python.contains(f"{cfg.python}.99", prereleases=True)
        ):
            raise ConfigError(
                f"python = {cfg.python!r} does not satisfy requires-python {requires_python}"
            )
    if "runtime" in tool:
        cfg.runtime = _str(tool["runtime"], "runtime")
        if cfg.runtime not in RUNTIMES:
            raise ConfigError(
                f"unsupported runtime {cfg.runtime!r}",
                hint="only 'python-build-standalone' is supported so far",
            )
    if "mode" in tool:
        cfg.mode = _str(tool["mode"], "mode")
        if cfg.mode not in MODES:
            raise ConfigError(f"mode must be one of {', '.join(MODES)}")
    if "icon" in tool:
        cfg.icon = root / _str(tool["icon"], "icon")
    if "lock" in tool:
        cfg.lock = root / _str(tool["lock"], "lock")
    if "exclude" in tool:
        cfg.exclude = _str_list(tool["exclude"], "exclude")
    if "zip" in tool:
        if not isinstance(tool["zip"], bool):
            raise ConfigError("[tool.pyxbuild] zip must be true or false")
        cfg.zip = tool["zip"]
    if "dist-dir" in tool:
        cfg.dist_dir = Path(_str(tool["dist-dir"], "dist-dir"))
    if "index-url" in tool:
        cfg.index_url = _str(tool["index-url"], "index-url")
    if "extra-index-urls" in tool:
        cfg.extra_index_urls = _str_list(tool["extra-index-urls"], "extra-index-urls")
    if "cross-compile" in tool:
        value = tool["cross-compile"]
        if isinstance(value, list):
            value = [canonicalize_name(_str(v, "cross-compile")) for v in value]
        elif not isinstance(value, bool):
            raise ConfigError("[tool.pyxbuild] cross-compile must be true, false or a list "
                              "of package names")
        cfg.cross_compile = value
    if "pure-python" in tool:
        from .fallbacks import PURE_PYTHON_SWITCHES

        cfg.pure_python = [canonicalize_name(v) for v in _str_list(tool["pure-python"],
                                                                     "pure-python")]
        unknown_pure = [p for p in cfg.pure_python if p not in PURE_PYTHON_SWITCHES]
        if unknown_pure:
            raise ConfigError(
                f"no known pure-Python build switch for: {', '.join(unknown_pure)}",
                hint=f"known: {', '.join(sorted(PURE_PYTHON_SWITCHES))}; for other packages "
                     "use allow-sdist (pure sdists) or cross-compile",
            )
    if "find-links" in tool:
        # relative directories are relative to the project, like other paths
        cfg.find_links = [
            v if "://" in v else str((root / v).resolve())
            for v in _str_list(tool["find-links"], "find-links")
        ]
    if "allow-sdist" in tool:
        if not isinstance(tool["allow-sdist"], bool):
            raise ConfigError("[tool.pyxbuild] allow-sdist must be true or false")
        cfg.allow_sdist = tool["allow-sdist"]
    if "extras" in tool:
        cfg.extras = _str_list(tool["extras"], "extras")
    if "utf8-mode" in tool:
        if not isinstance(tool["utf8-mode"], bool):
            raise ConfigError("[tool.pyxbuild] utf8-mode must be true or false")
        cfg.utf8_mode = tool["utf8-mode"]
    if "version-info" in tool:
        vi = tool["version-info"]
        if not isinstance(vi, dict) or not all(isinstance(v, str) for v in vi.values()):
            raise ConfigError("[tool.pyxbuild] version-info must be a table of strings")
        cfg.version_info = dict(vi)
    return cfg
