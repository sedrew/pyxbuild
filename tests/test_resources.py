import struct
import sys
import zlib

import pefile
import pytest

from pyxbuild import pe
from pyxbuild.config import EntryPoint
from pyxbuild.errors import ConfigError
from pyxbuild.launcher import build_launcher, decode_manifest, stub_bytes
from pyxbuild.resources import (
    RT_GROUP_ICON,
    RT_ICON,
    RT_MANIFEST,
    RT_VERSION,
    add_resources,
    launcher_resources,
    pe_checksum,
    read_ico,
)


def png(width: int, height: int, rgba=(255, 0, 0, 255)) -> bytes:
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    raw = b"".join(b"\0" + bytes(rgba) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def make_ico(path, sizes=(16, 32, 256)):
    images = [png(s, s) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for s, data in zip(sizes, images, strict=True):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.write_bytes(head + entries + b"".join(images))
    return path


def resources_of(data: bytes) -> dict:
    p = pefile.PE(data=data)
    out = {}
    for t in p.DIRECTORY_ENTRY_RESOURCE.entries:
        for e in t.directory.entries:
            for lang in e.directory.entries:
                d = lang.data.struct
                out[(t.id, e.id)] = p.get_data(d.OffsetToData, d.Size)
    return out


@pytest.fixture
def res(tmp_path):
    return launcher_resources(
        script="myapp", project_name="MyApp", version="1.2.3",
        description="My application",
        version_info_cfg={"company-name": "ACME", "copyright": "(c) 2026 ACME"},
        icon=make_ico(tmp_path / "app.ico"),
    )


def test_added_section_is_valid_pe(res):
    exe = add_resources(stub_bytes("win_amd64", gui=True), res)
    p = pefile.PE(data=exe)
    assert p.sections[-1].Name.rstrip(b"\0") == b".rsrc"
    assert p.OPTIONAL_HEADER.CheckSum == p.generate_checksum()
    assert pe_checksum(exe, p.OPTIONAL_HEADER.get_file_offset() + 64) == p.generate_checksum()
    assert p.OPTIONAL_HEADER.SizeOfImage == (
        p.sections[-1].VirtualAddress + p.sections[-1].Misc_VirtualSize + 0xFFF) // 0x1000 * 0x1000
    info = pe.parse(exe)  # our own parser still reads imports
    assert any(i.lower() == "kernel32.dll" for i in info.imports)
    assert info.subsystem == pe.SUBSYSTEM_GUI


def test_version_info_strings_and_fixed_info(res):
    exe = add_resources(stub_bytes("win_amd64", gui=False), res)
    p = pefile.PE(data=exe)
    fixed = p.VS_FIXEDFILEINFO[0]
    assert fixed.Signature == 0xFEEF04BD
    assert (fixed.FileVersionMS, fixed.FileVersionLS) == ((1 << 16) | 2, 3 << 16)
    strings = {}
    for fi in p.FileInfo[0]:
        if fi.Key == b"StringFileInfo":
            for st in fi.StringTable:
                strings.update({k.decode(): v.decode() for k, v in st.entries.items()})
        if fi.Key == b"VarFileInfo":
            assert fi.Var[0].entry == {b"Translation": "0x0409 0x04b0"}
    assert strings == {
        "CompanyName": "ACME", "FileDescription": "My application", "FileVersion": "1.2.3",
        "InternalName": "myapp", "LegalCopyright": "(c) 2026 ACME",
        "OriginalFilename": "myapp.exe", "ProductName": "MyApp", "ProductVersion": "1.2.3",
    }


def test_icon_and_manifest_resources(res, tmp_path):
    found = resources_of(add_resources(stub_bytes("win_amd64", gui=False), res))
    images = read_ico(tmp_path / "app.ico")
    assert [found[(RT_ICON, i + 1)] for i in range(3)] == [img.data for img in images]
    group = found[(RT_GROUP_ICON, 1)]
    assert struct.unpack_from("<HHH", group) == (0, 1, 3)
    assert struct.unpack_from("<BBBBHHIH", group, 6 + 14 * 2)[-1] == 3
    assert b"longPathAware" in found[(RT_MANIFEST, 1)]
    assert (RT_VERSION, 1) in found


def test_launcher_with_resources_keeps_manifest(res):
    exe = build_launcher(EntryPoint("myapp", "m", "f", False), "win_amd64", python_rel="runtime",
                         resources=res)
    assert decode_manifest(exe)["entry-point"] == "m:f"
    pefile.PE(data=exe)


def test_no_icon_is_fine():
    r = launcher_resources(script="a", project_name="a", version="0.1.dev3", description=None,
                           version_info_cfg={}, icon=None)
    assert set(r) == {RT_VERSION, RT_MANIFEST}
    exe = add_resources(stub_bytes("win_amd64", False), r)
    fixed = pefile.PE(data=exe).VS_FIXEDFILEINFO[0]
    assert (fixed.FileVersionMS, fixed.FileVersionLS) == (0 << 16 | 1, 0)


def test_bad_icon_and_keys(tmp_path):
    bad = tmp_path / "x.ico"
    bad.write_bytes(png(16, 16))
    with pytest.raises(ConfigError, match="not an .ico"):
        read_ico(bad)
    with pytest.raises(ConfigError, match="unknown key"):
        launcher_resources(script="a", project_name="a", version="1", description=None,
                           version_info_cfg={"colour": "red"}, icon=None)


@pytest.mark.skipif(sys.platform != "win32", reason="uses the Windows version API")
def test_windows_reads_version_and_icon(res, tmp_path):
    import ctypes
    from ctypes import wintypes

    exe = tmp_path / "myapp.exe"
    exe.write_bytes(build_launcher(EntryPoint("myapp", "m", "f", False), "win_amd64",
                                   python_rel="runtime", resources=res))
    ver = ctypes.windll.version
    size = ver.GetFileVersionInfoSizeW(str(exe), None)
    assert size
    buf = ctypes.create_string_buffer(size)
    assert ver.GetFileVersionInfoW(str(exe), 0, size, buf)
    ptr, n = ctypes.c_void_p(), wintypes.UINT()
    assert ver.VerQueryValueW(buf, r"\StringFileInfo\040904B0\CompanyName",
                              ctypes.byref(ptr), ctypes.byref(n))
    assert ctypes.wstring_at(ptr, n.value - 1) == "ACME"
    count = ctypes.windll.shell32.ExtractIconExW(str(exe), -1, None, None, 0)
    assert count == 1
