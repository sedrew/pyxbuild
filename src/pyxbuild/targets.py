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
    family: str = "windows"
    """``windows`` or ``linux``: runtime layout, launcher format, file name rules."""
    platform_tags: tuple[str, ...] = ()
    """Wheel platform tags, most preferred first (default: ``(name,)``)."""

    # --- runtime layout (python-build-standalone ``install_only``) -------------

    @property
    def is_windows(self) -> bool:
        return self.family == "windows"

    def stdlib_dir(self, python: str) -> str:
        """Standard library, relative to the runtime (``python``: ``"3.12"``)."""
        return "Lib" if self.is_windows else f"lib/python{python}"

    def site_packages(self, python: str) -> str:
        return f"{self.stdlib_dir(python)}/site-packages"

    @property
    def scripts_dir(self) -> str:
        return "Scripts" if self.is_windows else "bin"

    def interpreter(self, python: str, gui: bool = False) -> str:
        """The interpreter, relative to the runtime, with ``/``."""
        if self.is_windows:
            return "pythonw.exe" if gui else "python.exe"
        return f"bin/python{python}"

    def launcher_name(self, script: str) -> str:
        return f"{script}{self.exe_suffix}"

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
        # explicit abis: otherwise packaging adds "t"/"d" ABIs when pyxbuild
        # itself runs on a free-threaded or debug interpreter
        abis = [interpreter, "abi3", "none"]
        platforms = list(self.platform_tags or (self.name,))
        return [
            *cpython_tags(py, abis=abis, platforms=platforms),
            *compatible_tags(py, interpreter=interpreter, platforms=platforms),
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

# Linux: glibc 2.28 or newer (RHEL/Rocky 8, Debian 10, Ubuntu 20.04 and later);
# python-build-standalone itself needs 2.17. Wheels for any older manylinux
# baseline run there too.
GLIBC_MINOR = 28


def _manylinux(arch: str) -> tuple[str, ...]:
    tags = []
    for minor in range(GLIBC_MINOR, 4, -1):
        tags.append(f"manylinux_2_{minor}_{arch}")
        legacy = {17: "manylinux2014", 12: "manylinux2010", 5: "manylinux1"}.get(minor)
        if legacy:
            tags.append(f"{legacy}_{arch}")
    return tuple(tags)


LINUX_X86_64 = Target(
    name="linux_x86_64",
    uv_platform=f"x86_64-manylinux_2_{GLIBC_MINOR}",
    pbs_triple="x86_64-unknown-linux-gnu",
    os_name="posix",
    sys_platform="linux",
    platform_system="Linux",
    platform_machine="x86_64",
    exe_suffix="",
    family="linux",
    platform_tags=_manylinux("x86_64"),
)

LINUX_AARCH64 = Target(
    name="linux_aarch64",
    uv_platform=f"aarch64-manylinux_2_{GLIBC_MINOR}",
    pbs_triple="aarch64-unknown-linux-gnu",
    os_name="posix",
    sys_platform="linux",
    platform_system="Linux",
    platform_machine="aarch64",
    exe_suffix="",
    family="linux",
    platform_tags=_manylinux("aarch64"),
)

TARGETS = {t.name: t for t in (WIN_AMD64, LINUX_X86_64, LINUX_AARCH64)}


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
    elif isinstance(platform, str) and platform.startswith("linux-"):
        out.update(sys_platform="linux", os_name="posix", platform_machine=platform[6:])
    return out
