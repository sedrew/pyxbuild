"""The onefile launcher's DEFLATE decoder (stub/inflate.h), compiled for the
build host and compared with zlib."""

import os
import random
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "c" / "inflate_main.c"


def _compiler() -> list[str] | None:
    try:
        import ziglang  # noqa: F401
        return [sys.executable, "-m", "ziglang", "cc"]
    except ImportError:
        cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
        return [cc] if cc else None


@pytest.fixture(scope="module")
def inflate(tmp_path_factory):
    cc = _compiler()
    if cc is None:
        pytest.skip("no C compiler (pip install ziglang)")
    exe = tmp_path_factory.mktemp("inflate") / ("inflate.exe" if os.name == "nt" else "inflate")
    flags = ["-O2", "-Wall", "-Werror", "-fsanitize=undefined",
             "-fno-sanitize-recover=all"] if os.name != "nt" else ["-O2", "-Wall"]
    proc = subprocess.run([*cc, *flags, "-o", str(exe), str(HARNESS)],
                          capture_output=True, text=True)
    if proc.returncode != 0 and "-fsanitize=undefined" in flags:
        flags = ["-O2", "-Wall", "-Werror"]
        proc = subprocess.run([*cc, *flags, "-o", str(exe), str(HARNESS)],
                              capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    def run(compressed: bytes, size: int, tmp: Path) -> bytes | None:
        src, dst = tmp / "in.bin", tmp / "out.bin"
        src.write_bytes(compressed)
        p = subprocess.run([str(exe), str(src), str(dst), str(size)],
                           capture_output=True, text=True, timeout=60)
        assert p.returncode in (0, 1), (p.returncode, p.stdout, p.stderr)  # never crash
        return dst.read_bytes() if p.returncode == 0 else None
    return run


def raw_deflate(data: bytes, level: int = 9, strategy: int = zlib.Z_DEFAULT_STRATEGY) -> bytes:
    c = zlib.compressobj(level, zlib.DEFLATED, -15, 9, strategy)
    return c.compress(data) + c.flush()


def samples() -> dict[str, bytes]:
    rng = random.Random(1234)
    text = (ROOT / "src" / "pyxbuild" / "builder.py").read_bytes()
    return {
        "empty": b"",
        "one byte": b"x",
        "text": text,
        "repeats": b"abc" * 100_000,
        "zeros": bytes(1 << 20),
        "random": rng.randbytes(300_000),
        "mixed": b"".join(rng.randbytes(rng.randint(1, 500)) + b"A" * rng.randint(0, 3000)
                          for _ in range(400)),
        "binary": (ROOT / "src" / "pyxbuild" / "_stubs" / "launcher-console-win_amd64.exe"
                   ).read_bytes() * 20,
    }


@pytest.mark.parametrize("name", list(samples()))
@pytest.mark.parametrize(("level", "strategy"), [
    (9, zlib.Z_DEFAULT_STRATEGY), (1, zlib.Z_DEFAULT_STRATEGY), (0, zlib.Z_DEFAULT_STRATEGY),
    (6, zlib.Z_FIXED), (6, zlib.Z_HUFFMAN_ONLY), (6, zlib.Z_RLE)])
def test_matches_zlib(inflate, tmp_path, name, level, strategy):
    data = samples()[name]
    assert inflate(raw_deflate(data, level, strategy), len(data), tmp_path) == data


def test_output_buffer_too_small_is_an_error(inflate, tmp_path):
    data = b"hello world " * 1000
    assert inflate(raw_deflate(data), len(data) - 1, tmp_path) is None


def test_truncated_and_corrupted_input_never_crashes(inflate, tmp_path):
    rng = random.Random(7)
    data = samples()["text"]
    comp = raw_deflate(data)
    assert inflate(comp[: len(comp) // 2], len(data), tmp_path) is None
    for _ in range(60):
        bad = bytearray(comp)
        for _ in range(rng.randint(1, 8)):
            bad[rng.randrange(len(bad))] = rng.randrange(256)
        out = inflate(bytes(bad), len(data), tmp_path)
        assert out is None or len(out) <= len(data)
    for _ in range(40):  # garbage: an error or some output, but never a crash
        inflate(rng.randbytes(rng.randint(1, 2000)), 100_000, tmp_path)
