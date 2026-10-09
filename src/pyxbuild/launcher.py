"""Application launchers: a prebuilt stub plus an appended manifest.

Manifest layout (see ``stub/launcher.c``)::

    [stub PE image][payload][u32 payload length][u32 format version][b"PYXBLD01"]

The payload is UTF-8 ``key=value`` lines. The stub runs::

    "<launcher dir>\\<python>" <flags> -c "<code>" "<launcher path>" <user args>
"""

from __future__ import annotations

import struct
from importlib import resources

from .config import EntryPoint
from .errors import PyxbuildError

MAGIC = b"PYXBLD01"
FORMAT_VERSION = 1
TRAILER = struct.Struct("<II8s")

# Interpreter flags: -I isolates the app from PYTHON* variables, the user
# site-packages and the current directory (it implies -E -s -P). Because of
# -E, PYTHONIOENCODING/PYTHONUTF8 have no effect; use utf8-mode = true instead.
DEFAULT_FLAGS = "-I"


def stub_bytes(target: str, gui: bool) -> bytes:
    kind = "gui" if gui else "console"
    name = f"launcher-{kind}-{target}.exe"
    ref = resources.files("pyxbuild").joinpath("_stubs", name)
    try:
        return ref.read_bytes()
    except FileNotFoundError:
        raise PyxbuildError(f"launcher stub {name} is missing from this pyxbuild install") from None


def bootstrap_code(entry: EntryPoint) -> str:
    """One-line Python program run with ``-c`` for an entry point.

    The launcher passes its own path as the first argument; it becomes
    ``sys.argv[0]`` so the application sees the same ``argv`` as when run
    from a pip-installed console script.
    """
    code = (
        "import sys,importlib,functools;"
        "sys.argv[0:2]=sys.argv[1:2];"
        f"f=functools.reduce(getattr,{entry.attr.split('.')!r},"
        f"importlib.import_module({entry.module!r}));"
        "sys.exit(f())"
    )
    if '"' in code or "\n" in code:
        raise PyxbuildError(f"cannot encode entry point {entry.name}")
    return code


def encode_manifest(fields: dict[str, str]) -> bytes:
    lines = []
    for key, value in fields.items():
        if "\n" in key or "=" in key or "\n" in value or "\r" in value:
            raise PyxbuildError(f"invalid manifest field {key!r}")
        lines.append(f"{key}={value}\n")
    payload = "".join(lines).encode("utf-8")
    return payload + TRAILER.pack(len(payload), FORMAT_VERSION, MAGIC)


def decode_manifest(exe: bytes) -> dict[str, str]:
    """Inverse of :func:`encode_manifest` on a whole launcher (for tests and ``inspect``)."""
    end = len(exe)
    from .pe import security_directory

    sec = security_directory(exe)
    if sec is not None:
        end = sec[0]
    for _ in range(8):  # signing pads the overlay with up to 7 zero bytes
        if exe[end - len(MAGIC):end] == MAGIC or exe[end - 1] != 0:
            break
        end -= 1
    length, version, magic = TRAILER.unpack_from(exe, end - TRAILER.size)
    if magic != MAGIC:
        raise PyxbuildError("not a pyxbuild launcher (no manifest)")
    if version != FORMAT_VERSION:
        raise PyxbuildError(f"unsupported manifest version {version}")
    payload = exe[end - TRAILER.size - length:end - TRAILER.size].decode("utf-8")
    return dict(line.split("=", 1) for line in payload.splitlines() if line)


def build_launcher(entry: EntryPoint, target: str, *, python_rel: str,
                   utf8_mode: bool = False,
                   resources: dict[int, dict[int, bytes]] | None = None) -> bytes:
    """Stub (+ optional PE resources) + manifest for one entry point."""
    interpreter = python_rel + ("\\pythonw.exe" if entry.gui else "\\python.exe")
    fields = {
        "python": interpreter,
        "flags": DEFAULT_FLAGS + (" -X utf8" if utf8_mode else ""),
        "code": bootstrap_code(entry),
        "entry-point": f"{entry.module}:{entry.attr}",
    }
    stub = stub_bytes(target, entry.gui)
    if resources:
        from .resources import add_resources

        stub = add_resources(stub, resources)
    return stub + encode_manifest(fields)
