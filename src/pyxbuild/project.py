"""Build the project's own wheel (PEP 517, via ``uv build``) and read its metadata."""

from __future__ import annotations

import email.parser
import os
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

from packaging.utils import parse_wheel_filename

from .errors import PyxbuildError
from .lock import uv_executable


@dataclass(frozen=True)
class BuiltWheel:
    path: Path
    name: str
    version: str
    requires_dist: list[str]
    is_pure: bool


def read_wheel_metadata(path: Path) -> tuple[str, str, list[str]]:
    with zipfile.ZipFile(path) as zf:
        meta_name = next((n for n in zf.namelist()
                          if n.endswith(".dist-info/METADATA") and n.count("/") == 1), None)
        if meta_name is None:
            raise PyxbuildError(f"{path.name} has no .dist-info/METADATA")
        msg = email.parser.BytesParser().parsebytes(zf.read(meta_name), headersonly=True)
    return msg["Name"], msg["Version"], msg.get_all("Requires-Dist") or []


def build_wheel(source: Path, out_dir: Path, *, quiet: bool = False) -> BuiltWheel:
    """Build a wheel for ``source`` (a project directory or an sdist).

    Only pure-Python wheels can go into a cross-built application: a wheel
    with compiled code built on the host would contain host binaries.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    before = set(out_dir.glob("*.whl"))
    cmd = [uv_executable(), "build", "--wheel", "--out-dir", str(out_dir), str(source)]
    if quiet:
        cmd.append("--quiet")
    env = {**os.environ, "UV_NO_PROGRESS": "1"}
    env.setdefault("SOURCE_DATE_EPOCH", "315532800")
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise PyxbuildError(f"building a wheel for {source} failed:\n{proc.stderr.strip()}")
    built = sorted(set(out_dir.glob("*.whl")) - before) or sorted(out_dir.glob("*.whl"))
    if not built:
        raise PyxbuildError(f"uv build produced no wheel for {source}")
    wheel = max(built, key=lambda p: p.stat().st_mtime)
    _, _, _, tags = parse_wheel_filename(wheel.name)
    pure = all(t.abi == "none" and t.platform == "any" for t in tags)
    name, version, requires = read_wheel_metadata(wheel)
    return BuiltWheel(wheel, name, version, requires, pure)
