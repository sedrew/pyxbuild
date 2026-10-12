"""Onefile executables: the application folder as an archive inside the launcher.

File layout::

    [onefile launcher stub (+ resources)][archive][manifest][trailer]

Archive (all integers little-endian)::

    header   32 bytes  b"PYXARC01" | u32 version=1 | u32 chunk count |
                       u64 total stream size | u32 file count | u32 reserved
    table    16 bytes per chunk: u64 offset (from archive start) |
                       u32 stored size | u32 stream size
    chunks   raw DEFLATE (RFC 1951) of up to 1 MiB of the stream each, or the
             bytes as they are when the stored size equals the stream size

The stream is the concatenation of one record per file, in sorted order::

    u32 path length | u64 file size | path (UTF-8, "/"-separated) | file data

Chunks are compressed independently (in parallel, deterministic) and the
launcher needs only two chunk-sized buffers. ``stub/launcher.c`` extracts it;
``stub/inflate.h`` decodes DEFLATE without a C runtime.
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import os
import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .errors import PyxbuildError

MAGIC = b"PYXARC01"
VERSION = 1
CHUNK = 1 << 20
HEADER = struct.Struct("<8sIIQII")
ENTRY = struct.Struct("<QII")
RECORD = struct.Struct("<IQ")
MAX_PATH_BYTES = 4095


@dataclass(frozen=True)
class ArchiveInfo:
    path: Path
    size: int
    sha256: str
    files: int
    stream_size: int


def _files(root: Path, skip: set[str]) -> list[tuple[str, Path]]:
    out = []
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            rel = p.relative_to(root).as_posix()
            if rel not in skip:
                out.append((rel, p))
    return sorted(out)


def _stream(files: list[tuple[str, Path]]) -> Iterator[bytes]:
    for rel, path in files:
        name = rel.encode("utf-8")
        if not name or len(name) > MAX_PATH_BYTES:
            raise PyxbuildError(f"path too long for a onefile archive: {rel}")
        size = path.stat().st_size
        yield RECORD.pack(len(name), size) + name
        with open(path, "rb") as f:
            while block := f.read(CHUNK):
                yield block


def _chunks(stream: Iterator[bytes]) -> Iterator[bytes]:
    buf = bytearray()
    for piece in stream:
        buf += piece
        while len(buf) >= CHUNK:
            yield bytes(buf[:CHUNK])
            del buf[:CHUNK]
    if buf:
        yield bytes(buf)


def _compress(chunk: bytes, level: int) -> bytes:
    if level == 0:
        return chunk
    c = zlib.compressobj(level, zlib.DEFLATED, -15, 9)
    data = c.compress(chunk) + c.flush()
    # stored when compression does not help (wheels of already-compressed data)
    return data if len(data) < len(chunk) else chunk


def write_archive(root: Path, out: Path, *, skip: set[str] = frozenset(),
                  level: int = 9) -> ArchiveInfo:
    """Write the archive of ``root`` (minus ``skip``, relative POSIX paths) to ``out``."""
    files = _files(root, set(skip))
    stream_size = sum(RECORD.size + len(rel.encode("utf-8")) + p.stat().st_size
                      for rel, p in files)
    count = (stream_size + CHUNK - 1) // CHUNK
    table_end = HEADER.size + count * ENTRY.size
    entries = []
    offset = table_end
    with open(out, "wb") as f:
        f.write(b"\0" * table_end)
        workers = min(8, os.cpu_count() or 1)
        with cf.ThreadPoolExecutor(workers) as pool:  # zlib releases the GIL
            pending: list[tuple[cf.Future[bytes], int]] = []

            def drain(limit: int) -> None:
                nonlocal offset
                while len(pending) > limit:
                    fut, usize = pending.pop(0)
                    data = fut.result()
                    f.write(data)
                    entries.append(ENTRY.pack(offset, len(data), usize))
                    offset += len(data)

            for chunk in _chunks(_stream(files)):
                pending.append((pool.submit(_compress, chunk, level), len(chunk)))
                drain(workers * 2)
            drain(0)
        if len(entries) != count:
            raise PyxbuildError("files changed while the onefile archive was written")
        f.seek(0)
        f.write(HEADER.pack(MAGIC, VERSION, count, stream_size, len(files), 0))
        f.write(b"".join(entries))
    digest = hashlib.sha256()
    with open(out, "rb") as f:
        while block := f.read(1 << 20):
            digest.update(block)
    return ArchiveInfo(out, out.stat().st_size, digest.hexdigest(), len(files), stream_size)


def read_archive(data: bytes) -> dict[str, bytes]:
    """Decode an archive (tests and inspection; the launcher does this in C)."""
    magic, version, count, stream_size, nfiles, _ = HEADER.unpack_from(data, 0)
    if magic != MAGIC or version != VERSION:
        raise PyxbuildError("not a pyxbuild onefile archive")
    stream = bytearray()
    for i in range(count):
        off, csize, usize = ENTRY.unpack_from(data, HEADER.size + i * ENTRY.size)
        chunk = data[off:off + csize]
        stream += chunk if csize == usize else zlib.decompress(chunk, -15)
    if len(stream) != stream_size:
        raise PyxbuildError("corrupt onefile archive")
    files: dict[str, bytes] = {}
    pos = 0
    while pos < len(stream):
        plen, size = RECORD.unpack_from(stream, pos)
        pos += RECORD.size
        name = bytes(stream[pos:pos + plen]).decode("utf-8")
        pos += plen
        files[name] = bytes(stream[pos:pos + size])
        pos += size
    if len(files) != nfiles:
        raise PyxbuildError("corrupt onefile archive")
    return files
