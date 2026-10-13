"""Dependency resolution in pure Python, for hosts without uv (the browser).

``uv pip compile`` is the resolver everywhere uv runs. Where it cannot (in
Pyodide there are no processes), pyxbuild resolves with resolvelib, the
backtracking resolver of pip and PDM, over the PEP 691 JSON simple API of an
index (PyPI allows cross-origin requests to it). Dependencies come from the
PEP 658 ``.metadata`` files when the index has them, so only the chosen
wheels are ever downloaded. The result is the same kind of PEP 751 lock that
uv writes; the rest of the build does not know the difference.

Scope: wheels only (no sdists), one index, no find-links. Anything else
raises :class:`ResolveError` with a hint to use uv.
"""

from __future__ import annotations

import email.parser
import io
import json
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urljoin

import resolvelib
from packaging.markers import Marker
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import Tag
from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

from .errors import ResolveError

DEFAULT_INDEX = "https://pypi.org/simple"
SIMPLE_JSON = "application/vnd.pypi.simple.v1+json"


@dataclass(frozen=True)
class WheelFile:
    filename: str
    url: str
    sha256: str
    size: int | None
    metadata: str | bool | None  # PEP 658: sha256 of the .metadata file, True, or None
    tag_rank: int  # position of the best matching tag (lower is better)


@dataclass
class Candidate:
    name: str
    version: Version
    extras: frozenset[str]
    files: list[WheelFile]  # compatible wheels, best first
    _deps: list[Requirement] | None = field(default=None, repr=False)


class _Index:
    """The JSON simple API of one index, with per-run caches."""

    def __init__(self, url: str, get) -> None:
        self.url = url.rstrip("/") + "/"
        self._get = get
        self._pages: dict[str, list[dict]] = {}
        self._metadata: dict[str, bytes] = {}

    def files(self, name: str) -> list[dict]:
        if name not in self._pages:
            page_url = urljoin(self.url, f"{name}/")
            try:
                raw = self._get(page_url, {"Accept": SIMPLE_JSON})
            except Exception as exc:  # noqa: BLE001 - reported with the package name
                raise ResolveError(f"cannot read the index page of {name!r}: {exc}") from None
            try:
                data = json.loads(raw)
            except ValueError:
                raise ResolveError(
                    f"{page_url} did not return the JSON simple API (PEP 691)",
                    hint="the built-in resolver needs an index with the JSON API, like PyPI",
                ) from None
            files = data.get("files", [])
            for f in files:
                f["url"] = urljoin(page_url, f["url"])
            self._pages[name] = files
        return self._pages[name]

    def metadata(self, wheel: WheelFile) -> bytes:
        if wheel.url not in self._metadata:
            if wheel.metadata:
                data = self._get(wheel.url + ".metadata", {})
                if isinstance(wheel.metadata, str):
                    import hashlib

                    if hashlib.sha256(data).hexdigest() != wheel.metadata:
                        raise ResolveError(f"{wheel.filename}: .metadata hash mismatch")
            else:  # no PEP 658 metadata: read it from the wheel itself
                data = _metadata_from_wheel(self._get(wheel.url, {}), wheel.filename)
            self._metadata[wheel.url] = data
        return self._metadata[wheel.url]


def _metadata_from_wheel(data: bytes, filename: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist()
                 if n.count("/") == 1 and n.endswith(".dist-info/METADATA")]
        if not names:
            raise ResolveError(f"{filename} has no METADATA")
        return zf.read(names[0])


def _requires_dist(metadata: bytes) -> list[str]:
    msg = email.parser.BytesParser().parsebytes(metadata, headersonly=True)
    return msg.get_all("Requires-Dist") or []


class _Provider(resolvelib.AbstractProvider):
    """The resolvelib provider: candidates are versions with a wheel for the target."""

    def __init__(self, index: _Index, tags: list[Tag], env: dict[str, str],
                 python_version: Version) -> None:
        self.index = index
        self.rank = {t: i for i, t in enumerate(tags)}
        self.env = env
        self.python = python_version

    # identifiers: "name" or "name[extra1,extra2]"
    def identify(self, requirement_or_candidate) -> str:
        extras = requirement_or_candidate.extras
        name = canonicalize_name(requirement_or_candidate.name)
        return f"{name}[{','.join(sorted(extras))}]" if extras else name

    def get_preference(self, identifier, resolutions, candidates, information,
                       backtrack_causes) -> tuple:
        pinned = any(
            any(s.operator in ("==", "===") for s in info.requirement.specifier)
            for info in information[identifier]
        )
        caused = any(info.requirement.name == identifier for info in backtrack_causes)
        return (not caused, not pinned, identifier)

    def _versions(self, name: str) -> dict[Version, list[WheelFile]]:
        by_version: dict[Version, list[WheelFile]] = {}
        for f in self.index.files(name):
            filename = f.get("filename", "")
            if not filename.endswith(".whl") or f.get("yanked"):
                continue
            try:
                _, version, _, tags = parse_wheel_filename(filename)
            except (InvalidWheelFilename, InvalidVersion):
                continue
            ranks = [self.rank[t] for t in tags if t in self.rank]
            if not ranks:
                continue  # no wheel for the target
            rp = f.get("requires-python")
            if rp:
                try:
                    if not SpecifierSet(rp).contains(self.python, prereleases=True):
                        continue
                except ValueError:
                    pass  # unparsable requires-python: ignore it, like pip
            sha = (f.get("hashes") or {}).get("sha256")
            if not sha:
                continue
            meta = f.get("core-metadata", f.get("dist-info-metadata"))
            if isinstance(meta, dict):
                meta = meta.get("sha256") or True
            by_version.setdefault(version, []).append(WheelFile(
                filename, f["url"], sha, f.get("size"), meta or None, min(ranks)))
        for files in by_version.values():
            files.sort(key=lambda w: (w.tag_rank, w.filename))
        return by_version

    def find_matches(self, identifier, requirements, incompatibilities) -> list[Candidate]:
        name = identifier.split("[", 1)[0]
        reqs = list(requirements[identifier])
        extras = frozenset(e for r in reqs for e in r.extras)
        bad = {c.version for c in incompatibilities[identifier]}
        versions = self._versions(name)
        specifier = SpecifierSet()
        for r in reqs:
            specifier &= r.specifier
        explicit_pre = any(s.prereleases for r in reqs for s in r.specifier)
        ordered = sorted(versions, reverse=True)
        matching = [v for v in ordered if v not in bad
                    and specifier.contains(v, prereleases=True)]
        finals = [v for v in matching if not v.is_prerelease]
        chosen = matching if (explicit_pre or not finals) else finals
        return [Candidate(name, v, extras, versions[v]) for v in chosen]

    def is_satisfied_by(self, requirement, candidate: Candidate) -> bool:
        return requirement.specifier.contains(candidate.version, prereleases=True)

    def get_dependencies(self, candidate: Candidate) -> list[Requirement]:
        if candidate._deps is None:
            deps: list[Requirement] = []
            if candidate.extras:  # the extras candidate pins its base package
                deps.append(Requirement(f"{candidate.name}=={candidate.version}"))
            raw = _requires_dist(self.index.metadata(candidate.files[0]))
            for line in raw:
                try:
                    req = Requirement(line)
                except InvalidRequirement:
                    raise ResolveError(
                        f"{candidate.name} {candidate.version}: invalid requirement {line!r}"
                    ) from None
                if req.marker is not None and not self._marker_true(req.marker,
                                                                     candidate.extras):
                    continue
                if candidate.extras and not self._needs_extra(req.marker):
                    continue  # base dependencies come with the pinned base candidate
                req.marker = None
                deps.append(req)
            candidate._deps = deps
        return candidate._deps

    def _marker_true(self, marker: Marker, extras: frozenset[str]) -> bool:
        return any(marker.evaluate({**self.env, "extra": e}) for e in ("", *sorted(extras)))

    @staticmethod
    def _needs_extra(marker: Marker | None) -> bool:
        return marker is not None and "extra" in str(marker)


@dataclass(frozen=True)
class Pin:
    name: str
    version: str
    files: tuple[WheelFile, ...]


def resolve(requirements: Sequence[str], tags: list[Tag], env: dict[str, str],
            python_full_version: str, *, index_url: str | None = None, get=None,
            max_rounds: int = 2000) -> list[Pin]:
    """Resolve ``requirements`` for the target described by ``tags`` and ``env``."""
    from .download import http_get

    index = _Index(index_url or DEFAULT_INDEX, get or http_get)
    provider = _Provider(index, tags, env, Version(python_full_version))
    reqs = []
    for line in requirements:
        try:
            req = Requirement(line)
        except InvalidRequirement as exc:
            raise ResolveError(f"invalid requirement {line!r}: {exc}") from None
        if req.url:
            raise ResolveError(f"{line}: direct URL requirements need uv",
                               hint="build with the command-line pyxbuild")
        if req.marker is None or provider._marker_true(req.marker, frozenset()):
            req.marker = None
            reqs.append(req)
    resolver = resolvelib.Resolver(provider, resolvelib.BaseReporter())
    try:
        result = resolver.resolve(reqs, max_rounds=max_rounds)
    except resolvelib.ResolutionImpossible as exc:
        lines = []
        for cause in exc.causes:
            parent = f" (required by {cause.parent.name} {cause.parent.version})" \
                if cause.parent is not None else ""
            lines.append(f"    {cause.requirement}{parent}")
        raise ResolveError(
            "cannot resolve dependencies; no compatible wheel set for:\n" + "\n".join(lines),
            hint="a dependency may have no win_amd64 wheel for this Python; "
                 "pin another version or choose another 'python'",
        ) from None
    except resolvelib.ResolutionTooDeep:
        raise ResolveError("dependency resolution did not finish (too many rounds)") from None
    pins = {}
    for cand in result.mapping.values():
        pins[cand.name] = Pin(cand.name, str(cand.version), tuple(cand.files))
    return [pins[k] for k in sorted(pins)]


def _toml(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def pylock_text(pins: Iterable[Pin], index_url: str | None) -> str:
    """The resolution as PEP 751 TOML (the part uv would write)."""
    index = (index_url or DEFAULT_INDEX).rstrip("/")
    out = ['lock-version = "1.0"', 'created-by = "pyxbuild"']
    for pin in pins:
        out += ["", "[[packages]]", f"name = {_toml(pin.name)}",
                f"version = {_toml(pin.version)}", f"index = {_toml(index)}", "wheels = ["]
        for w in pin.files:
            size = f", size = {w.size}" if w.size is not None else ""
            out.append(f"    {{ name = {_toml(w.filename)}, url = {_toml(w.url)}{size}, "
                       f"hashes = {{ sha256 = {_toml(w.sha256)} }} }},")
        out.append("]")
    return "\n".join(out) + "\n"


def check_supported(*, find_links: Sequence[str], extra_index_urls: Sequence[str],
                    no_binary: Sequence[str]) -> None:
    unsupported = []
    if find_links:
        unsupported.append("find-links")
    if extra_index_urls:
        unsupported.append("extra-index-urls")
    if no_binary:
        unsupported.append("only-sdist")
    if unsupported:
        raise ResolveError(
            "the built-in resolver does not support " + ", ".join(unsupported),
            hint="build with the command-line pyxbuild, which resolves with uv")

