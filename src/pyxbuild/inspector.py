"""``pyxbuild inspect``: what a built application contains.

Reads a onefile executable (the archive the manifest points at, see
``onefile.py``), a patch executable (``patch.py``), an application folder or
its ``.zip``/``.tar.gz``. Paths are those of the application folder
(``runtime/Lib/site-packages/...``), as the onefile launcher extracts them.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import struct
import tarfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .errors import PyxbuildError
from .launcher import MAGIC as MANIFEST_MAGIC
from .launcher import decode_manifest
from .onefile import read_archive

INFO = "runtime/pyxbuild.json"


@dataclass
class Contents:
    """Files of one application (or of a patch's payload)."""

    path: Path
    kind: str                                # onefile, patch, folder, zip, tar
    sizes: dict[str, int]                    # relative POSIX path -> size
    read: Callable[[str], bytes]
    manifest: dict[str, str] = field(default_factory=dict)
    archive_size: int | None = None          # onefile: the archive inside the exe
    sha256: str | None = None                # onefile: of that archive; zip/tar: of the file

    def info(self) -> dict:
        """runtime/pyxbuild.json, if the application has one."""
        if INFO not in self.sizes:
            return {}
        try:
            return json.loads(self.read(INFO))
        except ValueError:
            return {}

    def patch(self) -> dict:
        return json.loads(self.read("patch.json"))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def _strip_top(names: list[str]) -> str:
    """The single top folder of a .zip/.tar.gz of an application (``myapp-win_amd64/``)."""
    tops = {n.split("/", 1)[0] for n in names}
    if len(tops) == 1 and all("/" in n for n in names):
        return tops.pop() + "/"
    return ""


def open_contents(path: Path) -> Contents:
    if path.is_dir():
        if not (path / INFO).is_file():
            raise PyxbuildError(f"{path} is not a pyxbuild application folder (no {INFO})",
                                hint="pass the folder that contains runtime/ and the "
                                     "launchers, e.g. dist/myapp-win_amd64")
        files = {p.relative_to(path).as_posix(): p for p in sorted(path.rglob("*"))
                 if p.is_file()}
        return Contents(path, "folder", {k: p.stat().st_size for k, p in files.items()},
                        lambda rel: files[rel].read_bytes())
    if not path.is_file():
        raise PyxbuildError(f"{path} not found")
    with open(path, "rb") as f:
        head = f.read(4)
    if head[:2] == b"MZ" or head == b"\x7fELF":
        return _executable(path)
    if head == b"PK\x03\x04":
        return _zip(path)
    if tarfile.is_tarfile(path):
        return _tar(path)
    raise PyxbuildError(f"{path} is not a pyxbuild executable, application folder, "
                        ".zip or .tar.gz")


def _executable(path: Path) -> Contents:
    data = path.read_bytes()
    try:
        manifest = decode_manifest(data)
    except (PyxbuildError, ValueError, IndexError, struct.error):
        hint = None
        if MANIFEST_MAGIC in data:
            hint = "the manifest is not at the end of the file: was it modified after the build?"
        raise PyxbuildError(f"{path} is not a pyxbuild executable", hint=hint) from None
    if "onefile" not in manifest:
        raise PyxbuildError(f"{path} is a launcher of a folder build: it carries no files",
                            hint="inspect the application folder (or its .zip) instead")
    # The manifest gives the archive's place; searching for b"PYXARC01" would
    # first find the launcher's own copy of the magic.
    off, size = int(manifest["archive-offset"]), int(manifest["archive-size"])
    if off + size > len(data):
        raise PyxbuildError(f"{path} is truncated: the archive ends after the file")
    blob = data[off:off + size]
    del data
    files = read_archive(blob)
    kind = "patch" if "patch-base" in manifest else "onefile"
    return Contents(path, kind, {k: len(v) for k, v in files.items()}, files.__getitem__,
                    manifest, size, hashlib.sha256(blob).hexdigest())


def _zip(path: Path) -> Contents:
    zf = zipfile.ZipFile(path)
    names = [n for n in zf.namelist() if not n.endswith("/")]
    top = _strip_top(names)
    infos = {i.filename[len(top):]: i for i in zf.infolist() if not i.filename.endswith("/")}
    return Contents(path, "zip", {k: i.file_size for k, i in infos.items()},
                    lambda rel: zf.read(infos[rel]), sha256=_sha256_file(path))


def _tar(path: Path) -> Contents:
    # read at once: a .tar.gz can only be decompressed front to back
    with tarfile.open(path) as tf:
        members = [m for m in tf.getmembers() if m.isfile()]
        top = _strip_top([m.name for m in members])
        files = {m.name[len(top):]: tf.extractfile(m).read() for m in members}
    return Contents(path, "tar", {k: len(v) for k, v in files.items()}, files.__getitem__,
                    sha256=_sha256_file(path))


def matches(rel: str, patterns: list[str]) -> bool:
    """fnmatch-style (``*`` also crosses ``/``); a pattern naming a folder matches
    everything below it."""
    if not patterns:
        return True
    parents = [str(p) for p in PurePosixPath(rel).parents][:-1]
    return any(fnmatch.fnmatchcase(c, pat) for pat in patterns for c in (rel, *parents))


def _mb(n: int) -> str:
    return f"{n / 2**20:.1f} MB" if n >= 2**20 else f"{n / 1024:.0f} KB"


# --- output -------------------------------------------------------------------

def summary(c: Contents) -> list[str]:
    if c.kind == "patch":
        return _patch_summary(c)
    info = c.info()
    target = info.get("target", "")
    lines = [f"{c.path}: {c.kind} application" + (f" ({target})" if target else "")]
    project = info.get("project", {})
    if project:
        lines.append(f"  project:  {project.get('name')} {project.get('version')}")
    if info.get("python"):
        lines.append(f"  python:   {info['python']}")
    if info.get("pyxbuild"):
        lines.append(f"  pyxbuild: {info['pyxbuild']}")
    if c.manifest:
        lines.append(f"  entry:    {c.manifest.get('entry-point')}")
        lines.append(f"  extract:  {c.manifest.get('extract')} (app {c.manifest.get('app')!r})")
    elif info.get("launchers"):
        lines.append("  scripts:  " + ", ".join(sorted(info["launchers"])))
    lines.append(f"  files:    {len(c.sizes)} ({_mb(sum(c.sizes.values()))})")
    if c.archive_size is not None:
        lines.append(f"  archive:  {_mb(c.archive_size)}, sha256 {c.sha256}")
        if c.manifest.get("onefile") != c.sha256:
            lines.append(f"  warning:  the manifest names archive {c.manifest.get('onefile')}: "
                         "the file was modified after the build")
    elif c.sha256:
        lines.append(f"  sha256:   {c.sha256}")
    if not info:
        lines.append(f"  note:     no {INFO}: not a complete pyxbuild application")
    return lines


def _patch_summary(c: Contents) -> list[str]:
    p = c.patch()
    files = p["files"]
    new = [e for e in files if e.get("new")]
    by_source = {s: sum(1 for e in files if e["from"] == s) for s in ("base", "payload", "wheel")}
    carried = sum(e["size"] for e in files if e["from"] == "payload")
    base = p["base"][:12] + (f" (version {p['base-version']})" if p.get("base-version") else "")
    lines = [f"{c.path}: patch for {p['app']}",
             f"  applies to: {base}",
             f"  updates to: {p['version']} ({p['target'][:12]})",
             f"  unchanged:  {len(p['keep'])} file(s)"]
    if "removed" in p:
        lines += [f"  added:      {len(new)} file(s)",
                  f"  replaced:   {len(files) - len(new)} file(s)",
                  f"  removed:    {len(p['removed'])} file(s)"]
    else:
        lines.append(f"  written:    {len(files)} file(s) (added or replaced; made by an older "
                     "pyxbuild, removed files are not recorded)")
    lines.append(f"  sources:    {by_source['payload']} carried ({_mb(carried)}), "
                 f"{by_source['base']} copied from the installed version, "
                 f"{by_source['wheel']} from {len(p.get('wheels', []))} downloaded wheel(s)")
    lines.append(f"  archive:    {_mb(c.archive_size or 0)}, sha256 {c.sha256}")
    return lines


def listing(c: Contents, patterns: list[str]) -> list[str]:
    if c.kind == "patch":
        return _patch_listing(c, patterns)
    rows = [(rel, size) for rel, size in sorted(c.sizes.items()) if matches(rel, patterns)]
    width = max([len(str(s)) for _, s in rows] + [1])
    out = [f"{size:>{width}}  {rel}" for rel, size in rows]
    out.append(f"{len(rows)} file(s), {_mb(sum(s for _, s in rows))}")
    return out


def _patch_listing(c: Contents, patterns: list[str]) -> list[str]:
    """``A``dded, ``M``odified (replaced), ``D``eleted, ``W``ritten (older patches)."""
    p = c.patch()
    known = "removed" in p
    rows = []
    for e in p["files"]:
        mark = ("A" if e.get("new") else "M") if known else "W"
        src = {"base": f"copied from {e.get('src')}", "payload": "carried",
               "wheel": "from a downloaded wheel"}[e["from"]]
        rows.append((e["path"], mark, e["size"], src))
    rows += [(path, "D", None, "") for path in p.get("removed", [])]
    rows = sorted(r for r in rows if matches(r[0], patterns))
    width = max([len(str(r[2])) for r in rows if r[2] is not None] + [1])
    out = [f"{mark} {'' if size is None else size:>{width}}  {path}"
           + (f"  ({src})" if src else "") for path, mark, size, src in rows]
    out.append(f"{len(rows)} change(s); {len(p['keep'])} unchanged file(s) not listed")
    return out


def _safe_dest(root: Path, rel: str) -> Path:
    parts = PurePosixPath(rel).parts
    if (not parts or rel.startswith("/") or ".." in parts or "\\" in rel
            or ":" in parts[0]):
        raise PyxbuildError(f"refusing to extract {rel!r}: outside the target folder")
    return root.joinpath(*parts)


def extract(c: Contents, dest: Path, patterns: list[str]) -> int:
    """Write the matching files below ``dest``; returns how many."""
    count = 0
    for rel in sorted(c.sizes):
        if not matches(rel, patterns):
            continue
        out = _safe_dest(dest, rel)
        out.parent.mkdir(parents=True, exist_ok=True)
        data = c.read(rel)
        out.write_bytes(data)
        if data[:4] == b"\x7fELF" or data[:2] == b"#!":  # as the Linux launcher does
            out.chmod(0o755)
        count += 1
    return count
