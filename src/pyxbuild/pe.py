"""Minimal, dependency-free reader for PE32+ files (``.exe``, ``.dll``, ``.pyd``).

Only what pyxbuild needs: machine type, subsystem, imported DLL names
(regular and delay-load) and the Authenticode security directory.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

IMAGE_FILE_MACHINE_AMD64 = 0x8664
IMAGE_FILE_MACHINE_I386 = 0x014C
IMAGE_FILE_MACHINE_ARM64 = 0xAA64

SUBSYSTEM_GUI = 2
SUBSYSTEM_CONSOLE = 3

_DIR_IMPORT = 1
_DIR_SECURITY = 4
_DIR_DELAY_IMPORT = 13


class PEError(ValueError):
    pass


@dataclass
class PEInfo:
    machine: int
    is_pe32_plus: bool
    subsystem: int
    is_dll: bool
    imports: list[str] = field(default_factory=list)
    delay_imports: list[str] = field(default_factory=list)


@dataclass
class _Section:
    va: int
    vsize: int
    raw_ptr: int
    raw_size: int


def _headers(data: bytes) -> tuple[int, int, bool]:
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise PEError("not an MZ executable")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise PEError("no PE signature")
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    if magic not in (0x10B, 0x20B):
        raise PEError(f"unknown optional header magic {magic:#x}")
    return pe, opt, magic == 0x20B


def _data_dir(data: bytes, opt: int, plus: bool, index: int) -> tuple[int, int]:
    base = opt + (112 if plus else 96)
    count = struct.unpack_from("<I", data, base - 4)[0]
    if index >= count:
        return 0, 0
    return struct.unpack_from("<II", data, base + 8 * index)


def security_directory(data: bytes) -> tuple[int, int] | None:
    """(file offset, size) of the Authenticode certificate table, if any."""
    try:
        _, opt, plus = _headers(data)
    except (PEError, struct.error):
        return None
    off, size = _data_dir(data, opt, plus, _DIR_SECURITY)
    return (off, size) if off and size else None


def parse(data: bytes) -> PEInfo:
    try:
        return _parse(data)
    except struct.error as exc:
        raise PEError(f"truncated PE file: {exc}") from None


def _parse(data: bytes) -> PEInfo:
    pe, opt, plus = _headers(data)
    machine, nsections = struct.unpack_from("<HH", data, pe + 4)
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    characteristics = struct.unpack_from("<H", data, pe + 22)[0]
    subsystem = struct.unpack_from("<H", data, opt + 68)[0]
    sections = []
    sec_off = opt + opt_size
    for i in range(nsections):
        vsize, va, raw_size, raw_ptr = struct.unpack_from("<IIII", data, sec_off + 40 * i + 8)
        sections.append(_Section(va, vsize, raw_ptr, raw_size))

    def rva_to_off(rva: int) -> int:
        for s in sections:
            if s.va <= rva < s.va + max(s.vsize, s.raw_size):
                off = rva - s.va + s.raw_ptr
                if off >= len(data):
                    break
                return off
        raise PEError(f"RVA {rva:#x} is outside every section")

    def cstr(rva: int) -> str:
        off = rva_to_off(rva)
        end = data.find(b"\0", off, off + 512)
        if end < 0:
            raise PEError("unterminated string")
        return data[off:end].decode("ascii", "replace")

    info = PEInfo(machine=machine, is_pe32_plus=plus, subsystem=subsystem,
                  is_dll=bool(characteristics & 0x2000))
    rva, size = _data_dir(data, opt, plus, _DIR_IMPORT)
    if rva and size:
        off = rva_to_off(rva)
        for _ in range(4096):
            name_rva = struct.unpack_from("<5I", data, off)[3]
            if name_rva == 0:
                break
            info.imports.append(cstr(name_rva))
            off += 20
    rva, size = _data_dir(data, opt, plus, _DIR_DELAY_IMPORT)
    if rva and size:
        off = rva_to_off(rva)
        for _ in range(4096):
            attrs, name_rva = struct.unpack_from("<II", data, off)
            if name_rva == 0:
                break
            # attributes bit 0 = RVA-based (always set on PE32+)
            info.delay_imports.append(cstr(name_rva))
            off += 32
    return info
