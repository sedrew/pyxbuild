"""Content-addressed download cache with sha256 verification."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from . import __version__
from .errors import DownloadError

USER_AGENT = f"pyxbuild/{__version__}"


def cache_dir() -> Path:
    env = os.environ.get("PYXBUILD_CACHE_DIR")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "pyxbuild" / "Cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "pyxbuild"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "pyxbuild"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_offline_override = False


@contextmanager
def offline_mode(enabled: bool = True):
    """Forbid downloads inside the block (``--offline``) without touching os.environ."""
    global _offline_override
    previous, _offline_override = _offline_override, enabled
    try:
        yield
    finally:
        _offline_override = previous


def offline() -> bool:
    return _offline_override or os.environ.get("PYXBUILD_OFFLINE", "") not in ("", "0")


def fetch(url: str, sha256: str, filename: str, *, quiet: bool = False) -> Path:
    """Return a local path to ``url``'s content, downloading it if not cached.

    Files are stored under ``<cache>/blobs/<sha256>/<filename>`` and written
    only after the hash matched, so anything in the cache is trusted.
    ``file://`` URLs and plain paths are supported for local indexes.
    """
    sha256 = sha256.lower()
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise DownloadError(f"invalid sha256 for {filename}: {sha256!r}")
    # the name comes from a lock file: it must be one plain path component
    if (not filename or filename in (".", "..") or any(c in filename for c in "/\\:\0")
            or filename != filename.strip()):
        raise DownloadError(f"refusing unsafe file name {filename!r}")
    blob_dir = cache_dir() / "blobs" / sha256
    dest = blob_dir / filename
    if not dest.resolve().is_relative_to(blob_dir.resolve()):
        raise DownloadError(f"refusing unsafe file name {filename!r}")
    if dest.is_file():
        return dest
    if offline():
        raise DownloadError(f"{filename} is not cached and PYXBUILD_OFFLINE is set")
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".part-")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            try:
                _download_to(url, out, quiet=quiet, filename=filename)
            except OSError as exc:  # local paths and file:// URLs
                raise DownloadError(f"cannot read {url}: {exc}") from None
        got = sha256_file(tmp)
        if got != sha256:
            raise DownloadError(
                f"hash mismatch for {filename}: expected {sha256}, got {got}",
                hint=f"source: {url}",
            )
        try:
            os.replace(tmp, dest)
        except PermissionError:
            # another process holds the finished file open (Windows); it has
            # the same verified content
            if not dest.is_file():
                raise
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def http_get(url: str, headers: dict[str, str] | None = None) -> bytes:
    """GET ``url`` into memory (index pages, metadata), with retries."""
    if sys.platform == "emscripten":
        return _browser_get(url, headers or {})
    last: Exception | None = None
    for attempt in range(4):
        if attempt:
            time.sleep(2**attempt)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404, 410):
                raise DownloadError(f"cannot download {url}: HTTP {exc.code}") from None
            last = exc
        except (urllib.error.URLError, OSError) as exc:
            last = exc
    raise DownloadError(f"cannot download {url}: {last}")


def _browser_get(url: str, headers: dict[str, str]) -> bytes:
    """Pyodide in a web worker: a synchronous XMLHttpRequest (allowed there),
    so the whole build can stay synchronous. The browser does TLS, proxies
    and caching; the server must allow cross-origin requests (PyPI does)."""
    from js import Uint8Array, XMLHttpRequest  # type: ignore[import-not-found]

    req = XMLHttpRequest.new()
    req.open("GET", url, False)
    req.responseType = "arraybuffer"
    for key, value in headers.items():
        req.setRequestHeader(key, value)
    try:
        req.send(None)
    except Exception as exc:  # noqa: BLE001 - a JsException: network or CORS failure
        raise DownloadError(f"cannot download {url}: {exc}") from None
    if req.status != 200:
        raise DownloadError(f"cannot download {url}: HTTP {req.status}")
    return bytes(Uint8Array.new(req.response).to_py())


def _download_to(url: str, out, *, quiet: bool, filename: str) -> None:
    if sys.platform == "emscripten" and "://" in url and not url.startswith("file://"):
        if not quiet:
            print(f"  downloading {filename}", file=sys.stderr)
        out.write(_browser_get(url, {}))
        return
    if url.startswith("file://") or "://" not in url:
        src = Path(urllib.request.url2pathname(url[7:])) if url.startswith("file://") else Path(url)
        with open(src, "rb") as f:
            shutil.copyfileobj(f, out)
        return
    if not quiet:
        print(f"  downloading {filename}", file=sys.stderr)
    last: Exception | None = None
    for attempt in range(4):
        if attempt:
            time.sleep(2**attempt)
            out.seek(0)
            out.truncate()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp:
                shutil.copyfileobj(resp, out, 1 << 20)
            return
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404, 410):
                raise DownloadError(f"cannot download {url}: HTTP {exc.code}") from None
            last = exc
        except (urllib.error.URLError, OSError) as exc:
            last = exc
    raise DownloadError(f"cannot download {url}: {last}")
