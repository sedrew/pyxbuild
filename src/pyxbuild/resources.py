"""Write Windows resources (icon, version info, application manifest) into a launcher.

The prebuilt stubs have no ``.rsrc`` section; this module appends one, so
every application gets its own launcher bytes (icon, VERSIONINFO) without a
resource compiler. Pure Python, no dependencies.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version

from .errors import ConfigError, PyxbuildError

RT_ICON = 3
RT_GROUP_ICON = 14
RT_VERSION = 16
RT_MANIFEST = 24
LANG_EN_US = 0x0409
CODEPAGE_UNICODE = 0x04B0
_DIR_RESOURCE = 2

APP_MANIFEST = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
    <security><requestedPrivileges>
      <requestedExecutionLevel level="asInvoker" uiAccess="false"/>
    </requestedPrivileges></security>
  </trustInfo>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1">
    <application><supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/></application>
  </compatibility>
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings>
      <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
    </windowsSettings>
  </application>
</assembly>
"""

# [tool.pyxbuild.version-info] keys -> StringFileInfo names
VERSION_STRING_KEYS = {
    "company-name": "CompanyName",
    "file-description": "FileDescription",
    "product-name": "ProductName",
    "copyright": "LegalCopyright",
    "trademarks": "LegalTrademarks",
    "comments": "Comments",
}


def _align(n: int, a: int) -> int:
    return (n + a - 1) // a * a


# --- icons -------------------------------------------------------------------------

@dataclass
class IconImage:
    width: int
    height: int
    colors: int
    planes: int
    bitcount: int
    data: bytes


def read_ico(path: Path) -> list[IconImage]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read icon {path}: {exc}") from None
    if len(raw) < 6:
        raise ConfigError(f"{path} is not an .ico file")
    reserved, kind, count = struct.unpack_from("<HHH", raw)
    if reserved != 0 or kind != 1 or count == 0:
        raise ConfigError(f"{path} is not an .ico file", hint="convert PNG/SVG to .ico first")
    images = []
    for i in range(count):
        w, h, colors, _, planes, bitcount, size, offset = struct.unpack_from(
            "<BBBBHHII", raw, 6 + 16 * i)
        if offset + size > len(raw):
            raise ConfigError(f"{path}: icon image {i} is truncated")
        images.append(IconImage(w, h, colors, planes, bitcount, raw[offset:offset + size]))
    return images


# --- VERSIONINFO -------------------------------------------------------------------

def _version_tuple(version: str) -> tuple[int, int, int, int]:
    try:
        release = Version(version).release
    except InvalidVersion:
        release = (0,)
    parts = (list(release) + [0, 0, 0, 0])[:4]
    return tuple(min(p, 0xFFFF) for p in parts)  # type: ignore[return-value]


def _block(key: str, value: bytes, children: bytes = b"", *, text: bool = False,
           value_len: int | None = None) -> bytes:
    """One VS_VERSIONINFO-style node: wLength, wValueLength, wType, szKey, Value, Children."""
    head = struct.pack("<HHH", 0, 0, 1 if text else 0) + (key + "\0").encode("utf-16-le")
    head += b"\0" * (_align(len(head), 4) - len(head))
    body = value + b"\0" * (_align(len(value), 4) - len(value)) if children else value
    node = head + body
    if children:
        node += children
    if value_len is None:
        value_len = len(value) // 2 if text else len(value)
    node = struct.pack("<HH", len(node), value_len) + node[4:]
    return node + b"\0" * (_align(len(node), 4) - len(node))


def version_info(*, file_version: str, product_version: str,
                 strings: dict[str, str]) -> bytes:
    fv = _version_tuple(file_version)
    pv = _version_tuple(product_version)
    fixed = struct.pack(
        "<13I",
        0xFEEF04BD, 0x00010000,
        (fv[0] << 16) | fv[1], (fv[2] << 16) | fv[3],
        (pv[0] << 16) | pv[1], (pv[2] << 16) | pv[3],
        0x3F, 0, 0x00040004, 1, 0, 0, 0,
    )
    table = b"".join(
        _block(k, (v + "\0").encode("utf-16-le"), text=True)
        for k, v in strings.items()
    )
    string_table = _block(f"{LANG_EN_US:04X}{CODEPAGE_UNICODE:04X}", b"", table, text=True)
    string_info = _block("StringFileInfo", b"", string_table, text=True)
    var = _block("Translation", struct.pack("<HH", LANG_EN_US, CODEPAGE_UNICODE))
    var_info = _block("VarFileInfo", b"", var, text=True)
    return _block("VS_VERSION_INFO", fixed, string_info + var_info, value_len=len(fixed))


# --- resource section ----------------------------------------------------------------

def _resource_section(resources: dict[int, dict[int, bytes]], section_rva: int) -> bytes:
    """Serialise {type: {id: data}} (all language LANG_EN_US) as an .rsrc section."""
    types = sorted(resources)
    n_ids = sum(len(resources[t]) for t in types)
    dir_size = 16
    entry = 8
    # layout: root dir, type dirs, id (language) dirs, data entries, data
    root_len = dir_size + entry * len(types)
    type_dirs_len = sum(dir_size + entry * len(resources[t]) for t in types)
    lang_dirs_len = n_ids * (dir_size + entry)
    entries_off = root_len + type_dirs_len + lang_dirs_len
    data_off = entries_off + 16 * n_ids

    out = bytearray()
    blobs = bytearray()

    def directory(count: int) -> bytes:
        return struct.pack("<IIHHHH", 0, 0, 0, 0, 0, count)

    out += directory(len(types))
    type_dir_off = root_len
    offsets = []
    for t in types:
        offsets.append(type_dir_off)
        out += struct.pack("<II", t, 0x80000000 | type_dir_off)
        type_dir_off += dir_size + entry * len(resources[t])
    lang_off = root_len + type_dirs_len
    data_entry = 0
    lang_dirs = bytearray()
    entries = bytearray()
    for t in types:
        out += directory(len(resources[t]))
        for rid in sorted(resources[t]):
            out += struct.pack("<II", rid, 0x80000000 | (lang_off + len(lang_dirs)))
            lang_dirs += directory(1) + struct.pack(
                "<II", LANG_EN_US, entries_off + 16 * data_entry)
            data = resources[t][rid]
            rva = section_rva + data_off + len(blobs)
            entries += struct.pack("<IIII", rva, len(data), 0, 0)
            blobs += data + b"\0" * (_align(len(data), 8) - len(data))
            data_entry += 1
    out += lang_dirs + entries
    assert len(out) == data_off, (len(out), data_off)
    return bytes(out + blobs)


def add_resources(exe: bytes, resources: dict[int, dict[int, bytes]]) -> bytes:
    """Return ``exe`` with an ``.rsrc`` section holding ``resources`` appended.

    ``exe`` must be a PE32+ image without overlay data and without an
    existing resource directory (true for the pyxbuild stubs).
    """
    data = bytearray(exe)
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    if struct.unpack_from("<H", data, opt)[0] != 0x20B:
        raise PyxbuildError("resources can only be added to PE32+ images")
    sect_align, file_align = struct.unpack_from("<II", data, opt + 32)
    headers_size = struct.unpack_from("<I", data, opt + 60)[0]
    res_dir = opt + 112 + 8 * _DIR_RESOURCE
    if struct.unpack_from("<I", data, res_dir)[0]:
        raise PyxbuildError("the launcher stub already has resources")
    sec_table = opt + opt_size
    new_hdr = sec_table + 40 * nsec
    if new_hdr + 40 > headers_size:
        raise PyxbuildError("no room for another section header in the launcher stub")
    end_va = end_raw = 0
    for i in range(nsec):
        vsize, va, raw_size, raw_ptr = struct.unpack_from("<IIII", data, sec_table + 40 * i + 8)
        end_va = max(end_va, va + max(vsize, raw_size))
        end_raw = max(end_raw, raw_ptr + raw_size)
    if end_raw != len(data):
        raise PyxbuildError("the launcher stub has trailing data; add resources first")

    va = _align(end_va, sect_align)
    payload = _resource_section(resources, va)
    raw_size = _align(len(payload), file_align)
    struct.pack_into("<8sIIIIIIHHI", data, new_hdr, b".rsrc\0\0\0", len(payload), va,
                     raw_size, end_raw, 0, 0, 0, 0, 0x40000040)
    struct.pack_into("<H", data, pe + 6, nsec + 1)
    struct.pack_into("<I", data, opt + 56, _align(va + len(payload), sect_align))  # SizeOfImage
    init = struct.unpack_from("<I", data, opt + 8)[0]
    struct.pack_into("<I", data, opt + 8, init + raw_size)  # SizeOfInitializedData
    struct.pack_into("<II", data, res_dir, va, len(payload))
    data += payload + b"\0" * (raw_size - len(payload))
    struct.pack_into("<I", data, opt + 64, pe_checksum(bytes(data), opt + 64))
    return bytes(data)


def pe_checksum(data: bytes, checksum_offset: int) -> int:
    """The PE optional-header checksum (as computed by ``MapFileAndCheckSum``)."""
    total = 0
    padded = data + b"\0" * (len(data) % 2)
    for i in range(0, len(padded), 2):
        if checksum_offset <= i < checksum_offset + 4:
            continue
        total += padded[i] | (padded[i + 1] << 8)
        total = (total & 0xFFFF) + (total >> 16)
    total = (total & 0xFFFF) + (total >> 16)
    return (total + len(data)) & 0xFFFFFFFF


def launcher_resources(*, script: str, project_name: str, version: str,
                       description: str | None, version_info_cfg: dict[str, str],
                       icon: Path | None) -> dict[int, dict[int, bytes]]:
    unknown = sorted(set(version_info_cfg) - set(VERSION_STRING_KEYS))
    if unknown:
        raise ConfigError(
            f"unknown key(s) in [tool.pyxbuild.version-info]: {', '.join(unknown)}",
            hint=f"known keys: {', '.join(VERSION_STRING_KEYS)}",
        )
    strings = {
        "FileDescription": description or project_name,
        "FileVersion": version,
        "InternalName": script,
        "OriginalFilename": f"{script}.exe",
        "ProductName": project_name,
        "ProductVersion": version,
    }
    for key, value in version_info_cfg.items():
        strings[VERSION_STRING_KEYS[key]] = value
    strings = dict(sorted(strings.items()))
    res: dict[int, dict[int, bytes]] = {
        RT_VERSION: {1: version_info(file_version=version, product_version=version,
                                     strings=strings)},
        RT_MANIFEST: {1: APP_MANIFEST.encode("utf-8")},
    }
    if icon is not None:
        images = read_ico(icon)
        res[RT_ICON] = {i + 1: img.data for i, img in enumerate(images)}
        group = struct.pack("<HHH", 0, 1, len(images)) + b"".join(
            struct.pack("<BBBBHHIH", img.width, img.height, img.colors, 0, img.planes,
                        img.bitcount, len(img.data), i + 1)
            for i, img in enumerate(images)
        )
        res[RT_GROUP_ICON] = {1: group}
    return res
