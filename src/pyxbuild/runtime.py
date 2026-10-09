"""Obtain a target CPython runtime (python-build-standalone ``install_only`` builds)."""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .download import cache_dir, fetch
from .errors import ConfigError, PyxbuildError
from .targets import Target, markers_from_build_details


@dataclass(frozen=True)
class RuntimeSpec:
    target: str
    python: str
    version: str
    url: str
    file: str
    sha256: str
    release: str


@dataclass(frozen=True)
class Runtime:
    spec: RuntimeSpec
    path: Path
    """Directory containing ``python.exe``."""

    def marker_overrides(self) -> dict[str, str]:
        for candidate in sorted(self.path.glob("**/build-details.json")):
            try:
                return markers_from_build_details(candidate)
            except (ValueError, OSError):
                continue
        return {}


def _table() -> dict:
    text = resources.files("pyxbuild").joinpath("runtimes.json").read_text(encoding="utf-8")
    return json.loads(text)


def runtime_spec(target: Target, python: str) -> RuntimeSpec:
    data = _table()
    entry = data["runtimes"].get(target.name, {}).get(python)
    if entry is None:
        available = ", ".join(sorted(data["runtimes"].get(target.name, {})))
        raise ConfigError(
            f"no pinned runtime for Python {python} on {target.name}",
            hint=f"available: {available}",
        )
    base = os.environ.get("PYXBUILD_RUNTIME_MIRROR", data["base_url"]).rstrip("/")
    url = f"{base}/{data['release']}/{entry['file'].replace('+', '%2B')}"
    return RuntimeSpec(
        target=target.name,
        python=python,
        version=entry["version"],
        url=url,
        file=entry["file"],
        sha256=entry["sha256"],
        release=data["release"],
    )


def _safe_extract(archive: Path, dest: Path) -> None:
    with tarfile.open(archive, "r:gz") as tf:
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")
            return
        root = dest.resolve()
        for member in tf.getmembers():  # Python < 3.11.4
            target = (dest / member.name).resolve()
            if not target.is_relative_to(root) or member.issym() or member.islnk():
                raise PyxbuildError(f"unsafe member {member.name!r} in {archive.name}")
        tf.extractall(dest)  # noqa: S202 - members validated above


def get_runtime(target: Target, python: str, *, quiet: bool = False) -> Runtime:
    spec = runtime_spec(target, python)
    root = cache_dir() / "runtimes" / spec.sha256
    python_dir = root / "python"
    if not (python_dir / f"python{target.exe_suffix}").is_file():
        archive = fetch(spec.url, spec.sha256, spec.file, quiet=quiet)
        root.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=root.parent, prefix=".extract-"))
        try:
            _safe_extract(archive, tmp)
            if not (tmp / "python" / f"python{target.exe_suffix}").is_file():
                raise PyxbuildError(f"{spec.file} does not contain python/python.exe")
            if (python_dir / f"python{target.exe_suffix}").is_file():
                pass  # another build finished first; never delete a runtime in use
            else:
                if root.exists():
                    shutil.rmtree(root)  # an incomplete earlier extraction
                try:
                    os.replace(tmp, root)
                except OSError:
                    if not (python_dir / f"python{target.exe_suffix}").is_file():
                        raise
        finally:
            if tmp.exists():
                shutil.rmtree(tmp)
    return Runtime(spec=spec, path=python_dir)
