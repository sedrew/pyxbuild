"""Resolve dependencies for a target and read/write ``pylock.toml`` (PEP 751).

Resolution is delegated to ``uv pip compile --python-platform`` which writes a
standard ``pylock.toml``. Reading the lock and picking one wheel per package
for the target is done here with :mod:`packaging`, so any PEP 751 lock file
(not only uv's) can drive a build.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from packaging.requirements import InvalidRequirement, Requirement
from packaging.tags import Tag
from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)

from .errors import ResolveError
from .targets import Target

LOCK_TOOL_TABLE = "tool.pyxbuild"

# uv settings that change a resolution; the lock records pyxbuild's own
# inputs only, so these must not leak in from the environment
UV_RESOLUTION_ENV = frozenset({
    "UV_PYTHON", "UV_INDEX", "UV_INDEX_URL", "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX_URL",
    "UV_FIND_LINKS", "UV_NO_INDEX", "UV_CONSTRAINT", "UV_OVERRIDE", "UV_BUILD_CONSTRAINT",
    "UV_EXCLUDE_NEWER", "UV_PRERELEASE", "UV_RESOLUTION", "UV_NO_BINARY", "UV_ONLY_BINARY",
    "UV_NO_BUILD", "UV_INDEX_STRATEGY", "UV_TORCH_BACKEND", "UV_CONFIG_FILE",
})


def uv_executable() -> str:
    env = os.environ.get("PYXBUILD_UV")
    if env:
        return env
    try:
        from uv import find_uv_bin

        return find_uv_bin()
    except (ImportError, FileNotFoundError):
        pass
    exe = shutil.which("uv")
    if exe is None:
        raise ResolveError("uv not found", hint="pip install uv")
    return exe


@dataclass(frozen=True)
class LockInputs:
    """Everything that determines the resolution; hashed to detect stale locks."""

    requirements: tuple[str, ...]
    target: str
    python_version: str
    index_url: str | None
    extra_index_urls: tuple[str, ...]
    allow_sdist: bool
    find_links: tuple[str, ...] = ()
    find_links_key: tuple[str, ...] = ()
    """find-links as written in the config: the digest must not depend on where
    the project is checked out."""
    no_binary: tuple[str, ...] = ()

    def digest(self) -> str:
        payload = json.dumps(
            {
                "requirements": sorted(self.requirements),
                "target": self.target,
                "python": self.python_version,
                "index-url": self.index_url,
                "extra-index-urls": list(self.extra_index_urls),
                "allow-sdist": self.allow_sdist,
                "find-links": list(self.find_links_key or self.find_links),
                "no-binary": sorted(self.no_binary),
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class LockedFile:
    name: str
    url: str
    sha256: str
    size: int | None = None


@dataclass(frozen=True)
class LockedPackage:
    name: str
    version: str | None
    marker: str | None
    wheels: tuple[LockedFile, ...]
    sdist: LockedFile | None
    other_source: str | None


@dataclass(frozen=True)
class Lock:
    path: Path
    packages: tuple[LockedPackage, ...]
    tool: dict

    @property
    def inputs_digest(self) -> str | None:
        return self.tool.get("inputs-sha256")


def requirement_lines(requires_dist: list[str], target: Target, env: dict[str, str],
                      extras: list[str], project: str | None = None) -> list[str]:
    """Turn ``Requires-Dist`` of the project into resolver input for the target.

    Markers are evaluated against the *target* environment (never the host);
    requirements gated by extras are kept only for the extras requested.
    """
    reqs = []
    for line in requires_dist:
        try:
            reqs.append(Requirement(line))
        except InvalidRequirement as exc:
            raise ResolveError(f"invalid requirement {line!r}: {exc}") from None
    own = canonicalize_name(project) if project else None
    wanted = {canonicalize_name(e) for e in extras} | {""}

    def applies(req: Requirement) -> bool:
        return req.marker is None or any(req.marker.evaluate({**env, "extra": e})
                                         for e in wanted)

    # "myapp[gui]; extra == 'all'" refers to the project itself: add its
    # extras to the wanted set instead of asking the index for "myapp"
    changed = True
    while changed and own:
        changed = False
        for req in reqs:
            if canonicalize_name(req.name) == own and applies(req):
                new = {canonicalize_name(e) for e in req.extras} - wanted
                if new:
                    wanted |= new
                    changed = True
    out = []
    for req in reqs:
        if canonicalize_name(req.name) == own or not applies(req):
            continue
        req.marker = None
        out.append(str(req))
    return sorted(set(out))


def compile_lock(inputs: LockInputs, target: Target, python_full_version: str,
                 out: Path, *, quiet: bool = False) -> None:
    """Run uv to resolve ``inputs`` for ``target`` and write ``out`` (PEP 751)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pyxbuild-lock-") as tmp:
        req_file = Path(tmp) / "requirements.in"
        req_file.write_text("".join(f"{r}\n" for r in inputs.requirements), encoding="utf-8")
        raw = Path(tmp) / "pylock.toml"
        cmd = [
            uv_executable(), "pip", "compile", str(req_file),
            "--python-platform", target.uv_platform,
            "--python-version", python_full_version,
            "--format", "pylock.toml",
            "--no-header",
            "--output-file", str(raw),
        ]
        if not inputs.allow_sdist:
            cmd += ["--only-binary", ":all:"]
        if inputs.index_url:
            cmd += ["--index-url", inputs.index_url]
        for url in inputs.extra_index_urls:
            cmd += ["--extra-index-url", url]
        for link in inputs.find_links:
            cmd += ["--find-links", link]
        for name in inputs.no_binary:
            cmd += ["--no-binary", name]
        if quiet:
            cmd.append("--quiet")
        cmd.append("--no-config")  # no uv.toml / [tool.uv] from the user or cwd
        env = {k: v for k, v in os.environ.items() if k not in UV_RESOLUTION_ENV}
        env["UV_NO_PROGRESS"] = "1"
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=tmp)
        if proc.returncode != 0:
            raise ResolveError(
                f"cannot resolve dependencies for {target.name} / Python {python_full_version}:\n"
                + _indent(proc.stderr.strip()),
                hint=_resolve_hint(proc.stderr, inputs),
            )
        text = raw.read_text(encoding="utf-8") if raw.exists() else 'lock-version = "1.0"\n'
    text = _localize_files(text, out.parent)
    out.write_text(_annotate(text, inputs, target, python_full_version), encoding="utf-8",
                   newline="\n")  # same lock bytes on every OS


def _indent(text: str) -> str:
    return "\n".join("    " + line for line in text.splitlines())


def _resolve_hint(stderr: str, inputs: LockInputs) -> str | None:
    if "has no wheels" in stderr or "no usable wheels" in stderr or "only-binary" in stderr:
        return ("a dependency has no wheel for this target. Options, safest first: pin a "
                "version that has one; lower 'python' in [tool.pyxbuild]; pure-python = "
                "[\"<pkg>\"] if its extension is optional; build the wheel on Windows CI and "
                "use find-links; allow-sdist = true for pure-Python sdists; cross-compile = "
                "[\"<pkg>\"] (experimental). See docs/NO_WHEEL.md")
    return None


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _annotate(text: str, inputs: LockInputs, target: Target, python_full_version: str) -> str:
    """Add ``environments`` and a ``[tool.pyxbuild]`` table to uv's output.

    Both are allowed by PEP 751: ``environments`` documents which environment
    the lock is valid for, ``[tool]`` carries tool-specific data.
    """
    env_marker = (
        f"sys_platform == '{target.sys_platform}' and "
        f"platform_machine == '{target.platform_machine}' and "
        f"python_full_version == '{python_full_version}'"
    )
    header = [
        "# Generated by pyxbuild (resolution by uv). Do not edit by hand.",
        "# Regenerate with: pyxbuild lock",
    ]
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    out: list[str] = []
    inserted = False
    for line in lines:
        out.append(line)
        if not inserted and line.startswith("lock-version"):
            out.append(f"environments = [{_toml_str(env_marker)}]")
            inserted = True
    if not inserted:
        out.insert(0, f"environments = [{_toml_str(env_marker)}]")
        out.insert(0, 'lock-version = "1.0"')
    while out and out[-1] == "":
        out.pop()
    tool = [
        "",
        "[tool.pyxbuild]",
        f"target = {_toml_str(target.name)}",
        f"python = {_toml_str(python_full_version)}",
        f"inputs-sha256 = {_toml_str(inputs.digest())}",
        "requirements = [" + ", ".join(_toml_str(r) for r in sorted(inputs.requirements)) + "]",
        f"resolver = {_toml_str(_uv_version())}",
    ]
    return "\n".join(header + out + tool) + "\n"


_LOCAL_FILE_RE = re.compile(
    r'url = "file://(?P<path>[^"]+)"(?P<mid>[^{}]*?)hashes = \{\s*\}')


def _localize_files(text: str, lock_dir: Path) -> str:
    """Make local find-links files portable and verifiable.

    uv records them as ``url = "file:///abs/path"`` with empty hashes. Files
    below the lock's directory become relative ``path`` entries (PEP 751)
    and every local file gets its sha256, so the lock works on any checkout
    and the files are verified like downloads.
    """
    def repl(m: re.Match[str]) -> str:
        local = Path(urllib.request.url2pathname(m["path"]))
        digest = hashlib.sha256(local.read_bytes()).hexdigest() if local.is_file() else None
        try:
            ref = f'path = "{local.resolve().relative_to(lock_dir.resolve()).as_posix()}"'
        except ValueError:
            ref = f'url = "file://{m["path"]}"'
        hashes = f'hashes = {{ sha256 = "{digest}" }}' if digest else "hashes = {}"
        return ref + m["mid"] + hashes

    return _LOCAL_FILE_RE.sub(repl, text)


def _uv_version() -> str:
    try:
        out = subprocess.run([uv_executable(), "--version"], capture_output=True, text=True)
        return out.stdout.strip() or "uv (unknown version)"
    except OSError:
        return "uv (unknown version)"


def _file_name(entry: dict) -> str:
    if "name" in entry:
        return entry["name"]
    url = entry.get("url") or entry.get("path") or ""
    return unquote(Path(urlparse(url).path).name)


def _locked_file(entry: dict, pkg: str) -> LockedFile:
    name = _file_name(entry)
    hashes = entry.get("hashes") or {}
    sha = hashes.get("sha256")
    if not sha:
        raise ResolveError(f"{pkg}: {name} in the lock file has no sha256 hash")
    url = entry.get("url") or entry.get("path")
    if not url:
        raise ResolveError(f"{pkg}: {name} in the lock file has neither url nor path")
    return LockedFile(name=name, url=url, sha256=sha, size=entry.get("size"))


def read_lock(path: Path) -> Lock:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ResolveError(f"cannot read lock file {path}: {exc}") from None
    version = str(data.get("lock-version", ""))
    if not version.startswith("1."):
        raise ResolveError(f"{path}: unsupported lock-version {version!r} (expected 1.x)")
    packages = []
    for raw in data.get("packages", []):
        name = raw.get("name")
        if not name:
            raise ResolveError(f"{path}: package without a name")
        other = next((k for k in ("vcs", "directory", "archive") if k in raw), None)
        wheels = tuple(_locked_file(w, name) for w in raw.get("wheels", []))
        sdist = _locked_file(raw["sdist"], name) if "sdist" in raw else None
        packages.append(LockedPackage(
            name=canonicalize_name(name), version=raw.get("version"),
            marker=raw.get("marker"), wheels=wheels, sdist=sdist, other_source=other,
        ))
    tool = data.get("tool", {}).get("pyxbuild", {})
    return Lock(path=path, packages=tuple(packages), tool=tool)


@dataclass(frozen=True)
class Selection:
    package: LockedPackage
    file: LockedFile
    is_wheel: bool
    tag: Tag | None


def select_files(lock: Lock, target: Target, env: dict[str, str], tags: list[Tag],
                 *, allow_sdist: bool = False) -> list[Selection]:
    """Pick, for every package that applies to the target, the best wheel.

    Wheels are ranked by the target's tag priority, like an installer running
    on the target would do. Packages whose marker is false are skipped.
    """
    priority = {t: i for i, t in enumerate(tags)}
    selected: list[Selection] = []
    problems: list[str] = []
    for pkg in lock.packages:
        if not target.evaluate(pkg.marker, env):
            continue
        if pkg.other_source:
            problems.append(f"{pkg.name}: {pkg.other_source} sources are not supported")
            continue
        best: tuple[int, LockedFile, Tag] | None = None
        for wheel in pkg.wheels:
            try:
                _, _, _, wtags = parse_wheel_filename(wheel.name)
            except InvalidWheelFilename:
                problems.append(f"{pkg.name}: invalid wheel file name {wheel.name!r}")
                continue
            for t in wtags:
                rank = priority.get(t)
                if rank is not None and (best is None or rank < best[0]):
                    best = (rank, wheel, t)
        if best is not None:
            selected.append(Selection(pkg, best[1], True, best[2]))
        elif pkg.sdist is not None and allow_sdist:
            selected.append(Selection(pkg, pkg.sdist, False, None))
        elif pkg.sdist is not None:
            problems.append(
                f"{pkg.name} {pkg.version or ''}: only an sdist is available for "
                f"{target.name} (allow-sdist = true builds pure-Python sdists on the host)"
            )
        else:
            names = ", ".join(w.name for w in pkg.wheels) or "no files"
            problems.append(f"{pkg.name} {pkg.version or ''}: no wheel for "
                            f"{target.name} / {tags[0].interpreter} ({names})")
    if problems:
        raise ResolveError(
            "the lock file cannot be installed on the target:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )
    return sorted(selected, key=lambda s: s.package.name)

