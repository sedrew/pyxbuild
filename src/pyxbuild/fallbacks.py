"""What to do when a dependency has no wheel for the target.

``PURE_PYTHON_SWITCHES``: packages whose compiled extension is optional,
with the switch their build script reads to skip it (checked against each
project's setup.py / PEP 517 backend, October 2026). With
``pure-python = ["msgpack"]`` pyxbuild builds the sdist on the host with the
switch set and accepts the result only if it is a pure ``py3-none-any`` wheel.
"""

from __future__ import annotations

PURE_PYTHON_SWITCHES: dict[str, dict[str, str]] = {
    "msgpack": {"MSGPACK_PUREPYTHON": "1"},
    "aiohttp": {"AIOHTTP_NO_EXTENSIONS": "1"},
    "multidict": {"MULTIDICT_NO_EXTENSIONS": "1"},
    "frozenlist": {"FROZENLIST_NO_EXTENSIONS": "1"},
    "yarl": {"YARL_NO_EXTENSIONS": "1"},
    "propcache": {"PROPCACHE_NO_EXTENSIONS": "1"},
    "pyyaml": {"PYYAML_FORCE_LIBYAML": "0", "PYYAML_FORCE_CYTHON": "0"},
    "simplejson": {"DISABLE_SPEEDUPS": "1"},
    "wrapt": {"WRAPT_INSTALL_EXTENSIONS": "false"},
    "charset-normalizer": {},  # pure unless CHARSET_NORMALIZER_USE_CYTHON=1
}

# Environment that must not leak into a pure build (it would force or
# require the extension).
PURE_PYTHON_UNSET = ("CIBUILDWHEEL", "REQUIRE_SPEEDUPS", "CHARSET_NORMALIZER_USE_CYTHON",
                     "PYYAML_FORCE_LIBYAML_REQUIRED")


def pure_python_env(package: str, base: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in base.items() if k not in PURE_PYTHON_UNSET}
    env.update(PURE_PYTHON_SWITCHES.get(package, {}))
    return env


def held_back(packages: list[tuple[str, str]], tags, *, index: str = "https://pypi.org",
              timeout: float = 20.0) -> list[tuple[str, str, str, bool, bool]]:
    """Locked packages older than the index's latest release.

    Returns ``(name, locked, latest, has_wheel_for_target, has_sdist)``. When the
    latest release has no usable wheel, the resolver fell back to an older
    version because of the target, which users should know about.
    """
    import concurrent.futures as cf
    import json
    import urllib.request

    from packaging.utils import InvalidWheelFilename, parse_wheel_filename
    from packaging.version import InvalidVersion, Version

    tag_set = set(tags)

    def one(item):
        name, locked = item
        try:
            with urllib.request.urlopen(f"{index}/pypi/{name}/json", timeout=timeout) as r:
                data = json.load(r)
            latest = data["info"]["version"]
            if Version(latest) <= Version(locked):
                return None
        except (OSError, ValueError, KeyError, InvalidVersion):
            return None
        has_wheel = False
        has_sdist = any(f.get("packagetype") == "sdist" for f in data.get("urls", []))
        for f in data.get("urls", []):
            try:
                _, _, _, wtags = parse_wheel_filename(f["filename"])
            except (InvalidWheelFilename, KeyError):
                continue
            if wtags & tag_set:
                has_wheel = True
                break
        return name, locked, latest, has_wheel, has_sdist

    with cf.ThreadPoolExecutor(8) as pool:
        return sorted(r for r in pool.map(one, packages) if r)
