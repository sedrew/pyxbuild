"""Patch executables: ship only what changed between two onefile builds.

``pyxbuild patch --from old/app.exe --to new/app.exe`` writes a small
executable that turns the installed (extracted) old version into the new
one. Every file of the new version is taken from one of:

* ``base``     the old version already has this content (any path): reused
* ``payload``  carried in the patch (``blobs/<sha256>``)
* ``wheel``    with ``--fetch-public``: a file of a public wheel named in the
               lock file; the patch downloads the wheel from its URL and
               checks its sha256 (the application's own code always travels
               in the patch)

The patch runs ``_pyxbuild_patch`` (copied into its payload) with the old
version's Python. The new version lands in the folder the new full
executable would extract to, so both stay interchangeable.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from importlib.resources import files as resources_files
from pathlib import Path
from urllib.parse import urlparse

from .errors import PyxbuildError
from .launcher import DEFAULT_FLAGS, decode_manifest, encode_manifest, stub_bytes
from .onefile import read_archive, write_archive

PATCH_CODE = ("import os,sys;sys.path.insert(0,os.environ['PYX_PATCH_PAYLOAD']);"
              "import _pyxbuild_patch;sys.exit(_pyxbuild_patch.main())")


@dataclass(frozen=True)
class PatchInfo:
    path: Path
    size: int
    app: str
    version: str
    reused: int
    carried: int
    fetched: int
    carried_bytes: int


def _onefile(exe: Path) -> tuple[dict[str, str], dict[str, bytes]]:
    data = exe.read_bytes()
    try:
        manifest = decode_manifest(data)
    except PyxbuildError:
        raise PyxbuildError(f"{exe} is not a pyxbuild executable") from None
    if "onefile" not in manifest or "patch-base" in manifest:
        raise PyxbuildError(f"{exe} is not a onefile application (mode = \"onefile\")")
    off, size = int(manifest["archive-offset"]), int(manifest["archive-size"])
    return manifest, read_archive(data[off:off + size])


def _wheel_files(wheels: list[tuple[str, str, int | None, Path]]) -> dict[str, dict]:
    """sha256 of every installable member -> where to get it."""
    out: dict[str, dict] = {}
    for url, sha, size, local in wheels:
        with zipfile.ZipFile(local) as zf:
            for member in zf.namelist():
                top = member.split("/", 1)[0]
                if member.endswith("/") or top.endswith((".dist-info", ".data")):
                    continue  # metadata is rewritten on install; .data is relocated
                digest = hashlib.sha256(zf.read(member)).hexdigest()
                out.setdefault(digest, {"url": url, "wheel_sha256": sha,
                                        "wheel_size": size, "member": member})
    return out


def public_wheels(lock_path: Path, python: str) -> list[tuple[str, str, int | None, Path]]:
    """The win_amd64 wheels of ``lock_path`` that come from http(s) URLs."""
    from .download import fetch
    from .lock import read_lock, select_files
    from .targets import WIN_AMD64

    env = WIN_AMD64.marker_environment(python)
    minor = ".".join(python.split(".")[:2])
    out = []
    for sel in select_files(read_lock(lock_path), WIN_AMD64, env, WIN_AMD64.tags(minor)):
        if sel.file is None or not sel.is_wheel:
            continue
        if urlparse(sel.file.url).scheme not in ("https", "http"):
            continue  # find-links / private files travel in the patch
        local = fetch(sel.file.url, sel.file.sha256, sel.file.name, quiet=True)
        out.append((sel.file.url, sel.file.sha256, sel.file.size, local))
    return out


def make_patch(base_exe: Path, target_exe: Path, out: Path, *,
               wheels: list[tuple[str, str, int | None, Path]] | None = None) -> PatchInfo:
    """Write a patch executable from ``base_exe`` to ``target_exe``.

    ``wheels``: public wheels (url, sha256, size, local copy) whose files the
    patch may download instead of carrying (``--fetch-public``).
    """
    base_m, base_files = _onefile(base_exe)
    target_m, target_files = _onefile(target_exe)
    if "\\" not in base_m.get("python", "") or "\\" not in target_m.get("python", ""):
        raise PyxbuildError("patch executables are made for Windows applications only "
                            "(win_amd64)")
    if base_m["app"] != target_m["app"]:
        raise PyxbuildError(f"different applications: {base_m['app']!r} and "
                            f"{target_m['app']!r}")
    if base_m["onefile"] == target_m["onefile"]:
        raise PyxbuildError("both executables contain the same version")
    if base_m.get("extract") == "temp":
        raise PyxbuildError("the old version uses onefile-extract = \"temp\": nothing stays "
                            "installed to patch", hint='use onefile-extract = "cache"')
    info = json.loads(target_files["runtime/pyxbuild.json"])
    version = info["project"]["version"]

    in_base: dict[str, str] = {}
    base_sha: dict[str, str] = {}
    for path, data in sorted(base_files.items()):
        digest = hashlib.sha256(data).hexdigest()
        base_sha[path] = digest
        in_base.setdefault(digest, path)
    del base_files
    public = _wheel_files(wheels) if wheels else {}

    # same path, same content: listed by path only, checked by one digest
    keep, keep_digest = [], hashlib.sha256()
    urls: dict[str, int] = {}
    wheel_list, entries, blobs = [], [], {}
    counts = {"base": 0, "payload": 0, "wheel": 0}
    for path, data in sorted(target_files.items()):
        digest = hashlib.sha256(data).hexdigest()
        if base_sha.get(path) == digest:
            keep.append(path)
            keep_digest.update(digest.encode())
            counts["base"] += 1
            continue
        entry = {"path": path, "sha256": digest, "size": len(data)}
        if digest in in_base:
            entry.update({"from": "base", "src": in_base[digest]})
        elif digest in public:
            w = public[digest]
            if w["url"] not in urls:
                urls[w["url"]] = len(wheel_list)
                wheel_list.append({"url": w["url"], "sha256": w["wheel_sha256"],
                                   "size": w["wheel_size"]})
            entry.update({"from": "wheel", "src": [urls[w["url"]], w["member"]]})
        else:
            entry["from"] = "payload"
            blobs[digest] = data
        counts[entry["from"]] += 1
        entries.append(entry)
    patch = {"format": 1, "app": target_m["app"], "version": version,
             "base": base_m["onefile"], "target": target_m["onefile"],
             "keep": keep, "keep_sha256": keep_digest.hexdigest(),
             "wheels": wheel_list, "files": entries}

    with tempfile.TemporaryDirectory(prefix="pyxbuild-patch-") as tmp:
        root = Path(tmp) / "payload"
        (root / "blobs").mkdir(parents=True)
        for digest, data in blobs.items():
            (root / "blobs" / digest).write_bytes(data)
        (root / "patch.json").write_text(json.dumps(patch, separators=(",", ":")) + "\n",
                                         encoding="utf-8", newline="\n")
        script = resources_files("pyxbuild").joinpath("_boot", "_pyxbuild_patch.py")
        (root / "_pyxbuild_patch.py").write_bytes(script.read_bytes())
        archive = write_archive(root, Path(tmp) / "patch.pyxarc")
        stub = stub_bytes("win_amd64", gui=False, onefile=True)
        fields = {
            "python": "runtime\\python.exe",
            "flags": DEFAULT_FLAGS + " -X utf8",
            "code": PATCH_CODE,
            "entry-point": "pyxbuild-patch",
            "onefile": archive.sha256,
            "app": target_m["app"],
            "archive-offset": str(len(stub)),
            "archive-size": str(archive.size),
            "extract": "temp",
            "patch-base": base_m["onefile"],
        }
        partial = out.with_name(out.name + ".partial")
        with open(partial, "wb") as f:
            f.write(stub)
            with open(archive.path, "rb") as src:
                shutil.copyfileobj(src, f, 1 << 20)
            f.write(encode_manifest(fields))
        partial.replace(out)
    return PatchInfo(out, out.stat().st_size, target_m["app"], version, counts["base"],
                     counts["payload"], counts["wheel"], sum(len(b) for b in blobs.values()))


def patch_payload(patch_exe: Path) -> tuple[dict[str, str], dict[str, bytes]]:
    """Manifest and payload files of a patch executable (tests, inspection)."""
    data = patch_exe.read_bytes()
    manifest = decode_manifest(data)
    off, size = int(manifest["archive-offset"]), int(manifest["archive-size"])
    return manifest, read_archive(data[off:off + size])

