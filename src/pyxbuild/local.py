"""Local dependencies: uv workspace members and ``path`` sources.

A monorepo project depends on its siblings through ``[tool.uv.sources]``::

    [tool.uv.sources]
    core = { workspace = true }
    tools = { path = "../tools" }

Those packages are not on any index. pyxbuild builds their wheels from the
source folders, like the project's own wheel, installs them next to it and
hands only *their* dependencies to the resolver. The lock file therefore never
mentions them and stays portable; ``--frozen`` rebuilds them from the checkout.
"""

from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from .errors import PyxbuildError
from .project import BuiltWheel, build_wheel


def _read_pyproject(folder: Path) -> dict:
    path = folder / "pyproject.toml"
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PyxbuildError(f"cannot read {path}: {exc}") from None


def _uv_sources(data: dict) -> dict:
    return data.get("tool", {}).get("uv", {}).get("sources", {}) or {}


def find_workspace_root(start: Path) -> Path | None:
    """The nearest folder at or above ``start`` whose pyproject has [tool.uv.workspace]."""
    for folder in [start, *start.parents]:
        data = _read_pyproject(folder)
        if "workspace" in data.get("tool", {}).get("uv", {}):
            return folder
    return None


def workspace_members(root: Path) -> dict[str, Path]:
    """Canonical project name -> folder for every member of the uv workspace at ``root``."""
    ws = _read_pyproject(root).get("tool", {}).get("uv", {}).get("workspace", {})
    excludes = ws.get("exclude", [])
    members: dict[str, Path] = {}
    candidates = [root]
    for pattern in ws.get("members", []):
        candidates += sorted(p for p in root.glob(pattern) if p.is_dir())
    for folder in candidates:
        rel = folder.relative_to(root).as_posix()
        if any(fnmatch.fnmatch(rel, ex) for ex in excludes):
            continue
        name = _read_pyproject(folder).get("project", {}).get("name")
        if name:
            members.setdefault(canonicalize_name(name), folder.resolve())
    return members


@dataclass
class LocalPackages:
    """Result of following local sources from the project's requirements."""

    requirements: list[str]                      # what the resolver still has to find
    wheels: list[BuiltWheel] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return [canonicalize_name(w.name) for w in self.wheels]


class _Sources:
    """Local sources visible from a project: its own, then the workspace root's."""

    def __init__(self, project_root: Path) -> None:
        self.workspace = find_workspace_root(project_root)
        self._members: dict[str, Path] | None = None
        self.paths: dict[str, Path] = {}
        self.add(project_root)
        if self.workspace is not None and self.workspace != project_root:
            self.add(self.workspace)

    def members(self) -> dict[str, Path]:
        if self._members is None:
            self._members = workspace_members(self.workspace) if self.workspace else {}
        return self._members

    def add(self, folder: Path) -> None:
        """Register the local sources declared in ``folder``/pyproject.toml."""
        for raw_name, spec in _uv_sources(_read_pyproject(folder)).items():
            name = canonicalize_name(raw_name)
            if name in self.paths:
                continue
            if isinstance(spec, list):  # several sources with markers
                local = [s for s in spec if "path" in s or s.get("workspace")]
                if not local:
                    continue
                if len(local) > 1:
                    raise PyxbuildError(
                        f"{raw_name}: several local sources in [tool.uv.sources] are not "
                        "supported")
                spec = local[0]
            if "path" in spec:
                self.paths[name] = (folder / spec["path"]).resolve()
            elif spec.get("workspace"):
                member = self.members().get(name)
                if member is None:
                    raise PyxbuildError(
                        f"{raw_name} = {{ workspace = true }}, but no member of the uv "
                        f"workspace at {self.workspace or folder} is named {raw_name}")
                self.paths[name] = member

    def get(self, name: str) -> Path | None:
        return self.paths.get(canonicalize_name(name))


def resolve_local(project_root: Path, project_name: str, requires_dist: list[str],
                  extras: list[str], lines_for, out_dir: Path) -> LocalPackages:
    """Build local dependencies and collect the requirements left for the resolver.

    ``lines_for(requires_dist, extras, project)`` turns Requires-Dist into
    resolver lines for the target (markers evaluated, extras applied).
    """
    sources = _Sources(project_root)
    if not sources.paths:
        return LocalPackages(lines_for(requires_dist, extras, project_name))

    built: dict[str, BuiltWheel] = {}
    extras_seen: dict[str, set[str]] = {}
    external: set[str] = set()
    queue: list[tuple[list[str], list[str], str]] = [(requires_dist, extras, project_name)]
    own = canonicalize_name(project_name)
    while queue:
        reqs, wanted, owner = queue.pop()
        for line in lines_for(reqs, wanted, owner):
            req = Requirement(line)
            name = canonicalize_name(req.name)
            folder = sources.get(name)
            if folder is None:
                external.add(line)
                continue
            if name == own:
                continue
            new_extras = set(req.extras) - extras_seen.get(name, set())
            if name in built and not new_extras:
                _check_version(req, built[name])
                continue
            extras_seen.setdefault(name, set()).update(req.extras)
            if name not in built:
                if not folder.exists():
                    raise PyxbuildError(f"local source of {req.name} not found: {folder}")
                wheel = (_existing_wheel(folder) if folder.suffix == ".whl"
                         else build_wheel(folder, out_dir / name, quiet=True))
                if not wheel.is_pure:
                    raise PyxbuildError(
                        f"the local package {wheel.path.name} is platform-specific",
                        hint="build it for win_amd64 elsewhere (e.g. cibuildwheel) and add "
                             "the wheel through find-links instead of a local source")
                built[name] = wheel
                if folder.is_dir():
                    sources.add(folder)
            _check_version(req, built[name])
            queue.append((built[name].requires_dist, sorted(extras_seen[name]),
                          built[name].name))
    return LocalPackages(sorted(external), [built[n] for n in sorted(built)])


def _existing_wheel(path: Path) -> BuiltWheel:
    from packaging.utils import parse_wheel_filename

    from .project import read_wheel_metadata

    _, _, _, tags = parse_wheel_filename(path.name)
    name, version, requires, summary = read_wheel_metadata(path)
    pure = all(t.abi == "none" and t.platform == "any" for t in tags)
    return BuiltWheel(path, name, version, requires, pure, summary)


def _check_version(req: Requirement, wheel: BuiltWheel) -> None:
    if req.specifier and not req.specifier.contains(wheel.version, prereleases=True):
        raise PyxbuildError(f"{req} is required, but the local source builds "
                            f"{wheel.name} {wheel.version}")
