"""Build the project's wheel in-process, for hosts without uv (the browser).

The command-line pyxbuild builds the project with its own build backend
(``uv build``, PEP 517). In Pyodide nothing can be spawned, so simple
pure-Python projects are packed directly: the import package (``src/<pkg>``,
``<pkg>/``) or module (``<mod>.py``) named after the project, plus metadata
from ``[project]`` (static fields only). That is what hatchling or
setuptools produce for such a layout. Anything else raises a clear error.
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
import tomllib
import zipfile
from pathlib import Path

from packaging.utils import canonicalize_name

from .errors import PyxbuildError

_EPOCH = (1980, 1, 1, 0, 0, 0)


def _hint() -> str:
    return "build this project with the command-line pyxbuild (any OS), which runs its backend"


def _find_import(root: Path, name: str) -> tuple[Path, str]:
    """(path to the package dir or module file, its name in the wheel)."""
    module = canonicalize_name(name).replace("-", "_")
    for base in (root / "src", root):
        if (base / module / "__init__.py").is_file():
            return base / module, module
        if (base / f"{module}.py").is_file():
            return base / f"{module}.py", f"{module}.py"
    raise PyxbuildError(
        f"cannot find the code of {name!r}: expected src/{module}/, {module}/ or {module}.py",
        hint=_hint())


def _metadata(project: dict) -> str:
    lines = ["Metadata-Version: 2.3", f"Name: {project['name']}",
             f"Version: {project['version']}"]
    if project.get("description"):
        lines.append(f"Summary: {' '.join(str(project['description']).split())}")
    if project.get("requires-python"):
        lines.append(f"Requires-Python: {project['requires-python']}")
    for dep in project.get("dependencies", []):
        lines.append(f"Requires-Dist: {dep}")
    for extra, deps in sorted(project.get("optional-dependencies", {}).items()):
        lines.append(f"Provides-Extra: {extra}")
        for dep in deps:
            marker = f'extra == "{extra}"'
            if ";" in dep:
                req, _, cond = dep.partition(";")
                lines.append(f"Requires-Dist: {req.strip()} ; ({cond.strip()}) and {marker}")
            else:
                lines.append(f"Requires-Dist: {dep} ; {marker}")
    return "\n".join(lines) + "\n"


def _entry_points(project: dict) -> str | None:
    sections = []
    for key, section in (("scripts", "console_scripts"), ("gui-scripts", "gui_scripts")):
        items = project.get(key) or {}
        if items:
            sections.append(f"[{section}]\n" + "".join(f"{k} = {v}\n"
                                                       for k, v in sorted(items.items())))
    return "\n".join(sections) or None


def build_pure_wheel(root: Path, out_dir: Path) -> Path:
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PyxbuildError(f"cannot read {root / 'pyproject.toml'}: {exc}") from None
    project = data.get("project") or {}
    dynamic = set(project.get("dynamic", []))
    for key in ("name", "version"):
        if key in dynamic or not project.get(key):
            raise PyxbuildError(f"[project] {key} must be set statically here", hint=_hint())
    if dynamic & {"dependencies", "optional-dependencies", "scripts", "gui-scripts"}:
        raise PyxbuildError("dynamic [project] metadata needs the project's build backend",
                            hint=_hint())
    code, arcname = _find_import(root, project["name"])
    dist = re.sub(r"[-_.]+", "_", project["name"]).lower()
    version = str(project["version"])
    info = f"{dist}-{version}.dist-info"

    files: list[tuple[str, bytes]] = []
    if code.is_file():
        files.append((arcname, code.read_bytes()))
    else:
        for path in sorted(code.rglob("*")):
            rel = path.relative_to(code).as_posix()
            if path.is_dir() or "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
                continue
            files.append((f"{arcname}/{rel}", path.read_bytes()))
    files.append((f"{info}/METADATA", _metadata(project).encode()))
    files.append((f"{info}/WHEEL", b"Wheel-Version: 1.0\nGenerator: pyxbuild\n"
                                   b"Root-Is-Purelib: true\nTag: py3-none-any\n"))
    entry_points = _entry_points(project)
    if entry_points:
        files.append((f"{info}/entry_points.txt", entry_points.encode()))

    record = io.StringIO()
    for name, content in files:
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
        record.write(f"{name},sha256={digest.decode()},{len(content)}\n")
    record.write(f"{info}/RECORD,,\n")
    files.append((f"{info}/RECORD", record.getvalue().encode()))

    out_dir.mkdir(parents=True, exist_ok=True)
    wheel = out_dir / f"{dist}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files:
            zi = zipfile.ZipInfo(name, _EPOCH)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            zf.writestr(zi, content)
    return wheel
