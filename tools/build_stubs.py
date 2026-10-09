"""Build the Windows launcher stubs with ``zig cc``.

Usage::

    python tools/build_stubs.py            # rebuild into src/pyxbuild/_stubs
    python tools/build_stubs.py --check    # rebuild into a temp dir and
                                           # compare with the committed stubs

Zig is taken from the ``ziglang`` package on PyPI (``pip install ziglang``) or
from ``$ZIG`` / ``zig`` on PATH. The stubs are checked into the repository so
that pyxbuild itself needs no compiler; ``--check`` proves that the committed
binaries are exactly what the C source produces.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "stub" / "launcher.c"
OUT_DIR = ROOT / "src" / "pyxbuild" / "_stubs"

# The stubs are byte-reproducible only with the same zig release on every host.
# 0.16.0 is the newest ziglang on PyPI with wheels for Linux, macOS and Windows.
ZIG_VERSION = "0.16.0"

TARGETS = {
    "win_amd64": "x86_64-windows-gnu",
}

COMMON_FLAGS = [
    "-Os",
    "-s",
    "-fno-ident",
    "-ffile-prefix-map={root}=.",
    "-Wall",
]


def zig_command() -> list[str]:
    env = os.environ.get("ZIG")
    if env:
        return [env]
    try:
        import ziglang  # noqa: F401
    except ImportError:
        exe = shutil.which("zig")
        if exe is None:
            sys.exit("zig not found: pip install ziglang, or set $ZIG")
        return [exe]
    return [sys.executable, "-m", "ziglang"]


def build(out_dir: Path) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    zig = zig_command()
    version = subprocess.run([*zig, "version"], capture_output=True, text=True,
                             check=True).stdout.strip()
    if version != ZIG_VERSION:
        sys.exit(f"zig {version} found, but the stubs are built with zig {ZIG_VERSION}: "
                 f"pip install ziglang=={ZIG_VERSION}")
    hashes = {}
    for platform, triple in TARGETS.items():
        for kind in ("console", "gui"):
            out = out_dir / f"launcher-{kind}-{platform}.exe"
            flags = [f.format(root=ROOT) for f in COMMON_FLAGS]
            if kind == "gui":
                flags += ["-DPYX_GUI", "-Wl,--subsystem,windows"]
            else:
                flags += ["-Wl,--subsystem,console"]
            cmd = [*zig, "cc", "-target", triple, *flags, "-o", str(out),
                   os.path.relpath(SOURCE, ROOT)]
            subprocess.run(cmd, check=True, cwd=ROOT)
            for junk in out_dir.glob(out.stem + ".pdb"):
                junk.unlink()
            hashes[out.name] = hashlib.sha256(out.read_bytes()).hexdigest()
    return hashes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.check:
        for name, digest in build(OUT_DIR).items():
            print(f"{digest}  {name}")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        fresh = build(Path(tmp))
    status = 0
    for name, digest in fresh.items():
        committed = OUT_DIR / name
        have = (hashlib.sha256(committed.read_bytes()).hexdigest()
                if committed.exists() else "<missing>")
        ok = have == digest
        status |= not ok
        print(f"{'OK  ' if ok else 'DIFF'} {name} built={digest} committed={have}")
    return status


if __name__ == "__main__":
    sys.exit(main())
