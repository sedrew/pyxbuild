from __future__ import annotations

import base64
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden"


# --- running Windows executables ----------------------------------------------

def wine_command() -> str | None:
    """Wine binary to use: $PYXBUILD_WINE, else ``wine`` on PATH.

    Wine 9.0 (Ubuntu 24.04) lacks ucrtbase functions numpy needs; use Wine
    10+ (``tools/get_wine.sh`` downloads a portable build).
    """
    return os.environ.get("PYXBUILD_WINE") or shutil.which("wine")


def wine_available() -> bool:
    return sys.platform == "win32" or wine_command() is not None


def run_exe(exe: Path, *args: str, env: dict[str, str] | None = None, timeout: int = 600,
            cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a Windows executable natively on Windows, under Wine elsewhere.

    stdout and stderr are separate pipes: Wine fails to initialise Python's
    standard streams when both point at the same file.
    """
    full_env = {**os.environ, **(env or {})}
    if sys.platform == "win32":
        cmd = [str(exe), *args]
    else:
        wine = wine_command()
        if wine is None:
            pytest.skip("wine is not installed")
        cmd = [wine, str(exe), *args]
        full_env.setdefault("WINEDEBUG", "-all")
        # Behind a TLS-intercepting proxy the host CA bundle must reach the app.
        ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
        if ca and Path(ca).is_file():
            win_ca = "Z:" + str(Path(ca)).replace("/", "\\")
            full_env["REQUESTS_CA_BUNDLE"] = full_env["SSL_CERT_FILE"] = win_ca
        full_env.setdefault("WINEPREFIX", str(Path.home() / ".cache" / "pyxbuild-wine"))
    return subprocess.run(cmd, env=full_env, capture_output=True, text=True, timeout=timeout,
                          cwd=cwd, encoding="utf-8", errors="replace")


@pytest.fixture
def wine():
    if not wine_available():
        pytest.skip("needs Windows or Wine")
    return run_exe


# --- synthetic wheels -----------------------------------------------------------

def _record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def make_wheel(dest: Path, name: str, version: str = "1.0", *, tag: str = "py3-none-any",
               files: dict[str, bytes | str] | None = None, requires: list[str] | None = None,
               entry_points: str | None = None) -> Path:
    """Write a minimal but valid wheel (PEP 427) and return its path."""
    dist = name.replace("-", "_")
    info = f"{dist}-{version}.dist-info"
    contents: dict[str, bytes] = {}
    for path, data in (files or {f"{dist}/__init__.py": ""}).items():
        contents[path] = data.encode() if isinstance(data, str) else data
    meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    for r in requires or []:
        meta += f"Requires-Dist: {r}\n"
    contents[f"{info}/METADATA"] = meta.encode()
    contents[f"{info}/WHEEL"] = (f"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: "
                                 f"{'true' if tag.endswith('any') else 'false'}\nTag: {tag}\n"
                                 ).encode()
    if entry_points:
        contents[f"{info}/entry_points.txt"] = entry_points.encode()
    record = "".join(f"{p},{_record_hash(d)},{len(d)}\n" for p, d in contents.items())
    record += f"{info}/RECORD,,\n"
    contents[f"{info}/RECORD"] = record.encode()
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{dist}-{version}-{tag}.whl"
    with zipfile.ZipFile(path, "w") as zf:
        for p, d in contents.items():
            zf.writestr(p, d)
    return path


# --- synthetic PE files ----------------------------------------------------------

def make_pe(imports: list[str], *, machine: int = 0x8664, delay: list[str] | None = None,
            dll: bool = True, subsystem: int = 3) -> bytes:
    """Build a tiny PE32+ image whose only content is an import table."""
    file_align = sect_align = 0x200
    rva = 0x1000
    names = imports + (delay or [])
    # layout inside the section: descriptors, delay descriptors, then names
    desc_size = 20 * (len(imports) + 1)
    delay_size = 32 * (len(delay or []) + 1) if delay else 0
    name_off = desc_size + delay_size
    blob = bytearray()
    name_rvas = []
    strings = bytearray()
    for n in names:
        name_rvas.append(rva + name_off + len(strings))
        strings += n.encode() + b"\0"
    for i, _ in enumerate(imports):
        blob += struct.pack("<5I", 0, 0, 0, name_rvas[i], 0)
    blob += b"\0" * 20
    if delay:
        for j, _ in enumerate(delay):
            blob += struct.pack("<8I", 1, name_rvas[len(imports) + j], 0, 0, 0, 0, 0, 0)
        blob += b"\0" * 32
    blob += strings
    raw_size = (len(blob) + file_align - 1) // file_align * file_align
    blob += b"\0" * (raw_size - len(blob))

    pe_off = 0x80
    opt_size = 240
    headers_size = 0x200
    dos = bytearray(b"MZ" + b"\0" * (pe_off - 2))
    struct.pack_into("<I", dos, 0x3C, pe_off)
    characteristics = 0x0022 | (0x2000 if dll else 0)
    coff = struct.pack("<4sHHIIIHH", b"PE\0\0", machine, 1, 0, 0, 0, opt_size, characteristics)
    opt = bytearray(opt_size)
    struct.pack_into("<H", opt, 0, 0x20B)
    struct.pack_into("<I", opt, 32, sect_align)
    struct.pack_into("<I", opt, 36, file_align)
    struct.pack_into("<I", opt, 56, rva + sect_align)  # SizeOfImage
    struct.pack_into("<I", opt, 60, headers_size)
    struct.pack_into("<H", opt, 68, subsystem)
    struct.pack_into("<I", opt, 108, 16)  # NumberOfRvaAndSizes
    struct.pack_into("<II", opt, 112 + 8 * 1, rva, desc_size)
    if delay:
        struct.pack_into("<II", opt, 112 + 8 * 13, rva + desc_size, delay_size)
    section = struct.pack("<8sIIIIIIHHI", b".idata\0\0", raw_size, rva, raw_size, headers_size,
                          0, 0, 0, 0, 0xC0000040)
    header = bytes(dos) + coff + bytes(opt) + section
    header += b"\0" * (headers_size - len(header))
    return header + bytes(blob)


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    monkeypatch.setenv("PYXBUILD_CACHE_DIR", str(d))
    return d


@pytest.fixture(scope="session")
def shared_cache(tmp_path_factory):
    """A download cache shared by the slow tests of one session (or $PYXBUILD_CACHE_DIR)."""
    env = os.environ.get("PYXBUILD_CACHE_DIR")
    return Path(env) if env else tmp_path_factory.mktemp("pyxbuild-cache")


def file_digest_tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}

