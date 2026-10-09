"""Target platforms: marker environments (PEP 508) and wheel tags (PEP 425).

Everything here describes the machine the application will *run* on, never
the machine pyxbuild runs on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from packaging.markers import Marker
from packaging.tags import Tag, compatible_tags, cpython_tags
from packaging.version import Version

from .errors import ConfigError

SUPPORTED_PYTHONS = ("3.11", "3.12", "3.13", "3.14")


@dataclass(frozen=True)
class Target:
    """A platform pyxbuild can build applications for."""

    name: str
    """Wheel platform tag, e.g. ``win_amd64``; also the user-facing target name."""
    uv_platform: str
    """Value for ``uv pip compile --python-platform``."""
    pbs_triple: str
    """Target triple used in python-build-standalone file names."""
    os_name: str
    sys_platform: str
    platform_system: str
    platform_machine: str
    exe_suffix: str

    def marker_environment(
        self, python_full_version: str, overrides: dict[str, str] | None = None
    ) -> dict[str, str]:
        """PEP 508 environment of the target interpreter.

        ``overrides`` come from the runtime's ``build-details.json`` (PEP 739)
        when it ships one; they win over the built-in defaults.
        """
        v = Version(python_full_version)
        env = {
            "implementation_name": "cpython",
            "implementation_version": python_full_version,
            "os_name": self.os_name,
            "platform_machine": self.platform_machine,
            "platform_python_implementation": "CPython",
            "platform_release": "",
            "platform_system": self.platform_system,
            "platform_version": "",
            "python_full_version": python_full_version,
            "python_version": f"{v.major}.{v.minor}",
            "sys_platform": self.sys_platform,
        }
        if overrides:
            env.update(overrides)
        return env

    def tags(self, python_version: str) -> list[Tag]:
        """Supported wheel tags, most preferred first (like ``pip debug --verbose``)."""
        v = Version(python_version)
        py = (v.major, v.minor)
        interpreter = f"cp{v.major}{v.minor}"
        return [
            *cpython_tags(py, platforms=[self.name]),
            *compatible_tags(py, interpreter=interpreter, platforms=[self.name]),
        ]

    def evaluate(self, marker: str | Marker | None, env: dict[str, str]) -> bool:
        if marker is None or marker == "":
            return True
        m = marker if isinstance(marker, Marker) else Marker(marker)
        return m.evaluate({**env, "extra": ""})


WIN_AMD64 = Target(
    name="win_amd64",
    uv_platform="x86_64-pc-windows-msvc",
    pbs_triple="x86_64-pc-windows-msvc",
    os_name="nt",
    sys_platform="win32",
    platform_system="Windows",
    platform_machine="AMD64",
    exe_suffix=".exe",
)

TARGETS = {t.name: t for t in (WIN_AMD64,)}


def get_target(name: str) -> Target:
    try:
        return TARGETS[name]
    except KeyError:
        raise ConfigError(
            f"unknown target {name!r}",
            hint=f"supported targets: {', '.join(sorted(TARGETS))}",
        ) from None


def markers_from_build_details(path: Path) -> dict[str, str]:
    """Read marker values from a PEP 739 ``build-details.json`` file.

    Only the keys PEP 739 defines and that map 1:1 onto PEP 508 markers are
    used; anything missing falls back to the target's defaults.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    impl = data.get("implementation") or {}
    if isinstance(impl.get("name"), str):
        out["implementation_name"] = impl["name"]
    lang = data.get("language") or {}
    if isinstance(lang.get("version"), str):
        out["python_version"] = lang["version"]
    platform = data.get("platform")
    if platform == "win-amd64":
        out.update(sys_platform="win32", os_name="nt", platform_machine="AMD64")
    return out
