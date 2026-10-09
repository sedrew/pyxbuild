"""Experimental: cross-compile setuptools C/C++ extensions for Windows with zig.

When a dependency only ships an sdist, the usual answer is "build the wheel
on Windows". For extensions that do not need platform-specific build logic
(no ``sys.platform`` checks in setup.py, no external libraries) the build
can run on the host instead:

* setup.py, Cython, and packaging run on a host interpreter of the *same*
  ``X.Y`` version (uv provides one);
* compiling and linking is redirected to ``zig cc -target x86_64-windows-gnu``
  against the target runtime's ``include/`` and ``libs/python3XY.lib``
  (zig links the UCRT, the same C runtime MSVC-built CPython uses);
* the resulting wheel is re-tagged ``win_amd64`` and checked to contain only
  Windows PE binaries.

Only the C ABI is safe across toolchains: C++ extensions work as long as no
C++ objects cross into other MSVC-built libraries.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from importlib import resources
from pathlib import Path

from packaging.tags import Tag
from packaging.utils import parse_wheel_filename

from . import pe
from .download import cache_dir
from .errors import PyxbuildError
from .lock import uv_executable
from .project import BuiltWheel, read_wheel_metadata
from .runtime import Runtime

ZIG_TARGET = "x86_64-windows-gnu"

# Windows SDK spellings used in #include lines; mingw-w64 ships lower-case
# files and the host file system is case-sensitive.
SDK_HEADER_SPELLINGS = """
AclAPI.h BaseTsd.h BCrypt.h CommCtrl.h CommDlg.h DbgHelp.h Dbghelp.h Direct.h DXGI.h
ImageHlp.h Intrin.h IPHlpApi.h Iphlpapi.h IPTypes.h KnownFolders.h LM.h LMCons.h Lmcons.h
MMSystem.h Mmsystem.h MSWSock.h Mswsock.h NTSecAPI.h Ntsecapi.h ObjBase.h Objbase.h
OleAuto.h Pdh.h PowrProf.h Process.h Psapi.h Rpc.h Sddl.h SetupAPI.h ShellAPI.h
Shellapi.h ShlObj.h Shlobj.h Shlwapi.h ShlWApi.h TlHelp32.h Tlhelp32.h UserEnv.h
Userenv.h VersionHelpers.h WS2tcpip.h Ws2tcpip.h WinBase.h WinCrypt.h Wincrypt.h WinDef.h
WinError.h Winerror.h WinIoCtl.h Winioctl.h WinNT.h WinReg.h WinSock.h WinSock2.h
Winsock2.h WinUser.h Windows.h Winternl.h WtsApi32.h Wtsapi32.h
""".split()  # noqa: SIM905


def case_shim_dir(cache_root: Path) -> Path:
    shims = cache_root / "cross-include-shims"
    if not (shims / "Windows.h").is_file():
        shims.mkdir(parents=True, exist_ok=True)
        for name in SDK_HEADER_SPELLINGS:
            (shims / name).write_text(f"#include <{name.lower()}>\n", newline="\n")
    return shims
HOST_BINARY_SUFFIXES = (".so", ".dylib")


def zig_command() -> list[str]:
    env = os.environ.get("ZIG")
    if env:
        return [env]
    try:
        import ziglang  # noqa: F401

        return [sys.executable, "-m", "ziglang"]
    except ImportError:
        pass
    exe = shutil.which("zig")
    if exe:
        return [exe]
    raise PyxbuildError("cross-compiling needs zig",
                        hint="pip install 'pyxbuild[cross]' (installs ziglang from PyPI)")


def cross_build_wheel(source: Path, runtime: Runtime, out_dir: Path, *,
                      quiet: bool = False) -> BuiltWheel:
    """Build ``source`` (an sdist or project directory) into a win_amd64 wheel."""
    major, minor = runtime.spec.python.split(".")
    include = runtime.path / "include"
    libdir = runtime.path / "libs"
    pylib = f"python{major}{minor}"
    if not (libdir / f"{pylib}.lib").is_file() or not (include / "Python.h").is_file():
        raise PyxbuildError(f"runtime {runtime.spec.file} has no include/ or libs/{pylib}.lib")
    hook_dir = Path(str(resources.files("pyxbuild").joinpath("_cross_hook")))
    # The extension suffix travels in our own variable: SETUPTOOLS_EXT_SUFFIX
    # switches some setup.py files (zstandard) into their own cross paths.
    cfg = {"zig": zig_command(), "target": ZIG_TARGET, "include": str(include),
           "libdir": str(libdir), "pylib": pylib,
           "ext_suffix": f".cp{major}{minor}-win_amd64.pyd",
           "case_shims": str(case_shim_dir(cache_dir()))}
    env = {
        **os.environ,
        "PYXBUILD_CROSS": json.dumps(cfg),
        "PYTHONPATH": os.pathsep.join(filter(None, [str(hook_dir),
                                                    os.environ.get("PYTHONPATH")])),
        "UV_NO_PROGRESS": "1",
    }
    env.setdefault("SOURCE_DATE_EPOCH", "315532800")
    with tempfile.TemporaryDirectory(prefix="pyxbuild-cross-") as tmp:
        cmd = [uv_executable(), "build", "--wheel", "--python", f"{major}.{minor}",
               "--out-dir", tmp, str(source)]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-25:])
            raise PyxbuildError(f"cross-compiling {Path(source).name} failed:\n{tail}",
                                hint="setup.py may choose sources by sys.platform or need "
                                     "external libraries; build this wheel on Windows instead")
        built = sorted(Path(tmp).glob("*.whl"))
        if not built:
            raise PyxbuildError(f"cross build of {Path(source).name} produced no wheel")
        out_dir.mkdir(parents=True, exist_ok=True)
        tags = parse_wheel_filename(built[0].name)[3]
        if all(t.abi == "none" and t.platform == "any" for t in tags):
            wheel = Path(shutil.copy2(built[0], out_dir))  # pure Python after all
        else:
            wheel = retag_wheel(built[0], out_dir, platform="win_amd64")
    name, version, requires, summary = read_wheel_metadata(wheel)
    return BuiltWheel(wheel, name, version, requires, False, summary)


def _record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + digest.decode()


def retag_wheel(wheel: Path, out_dir: Path, *, platform: str) -> Path:
    """Copy ``wheel`` with its platform tag replaced; refuse host binaries."""
    name, version, build, tags = parse_wheel_filename(wheel.name)
    new_tags = sorted({Tag(t.interpreter, t.abi, platform) for t in tags}, key=str)
    with zipfile.ZipFile(wheel) as zf:
        entries = {i.filename: zf.read(i) for i in zf.infolist() if not i.is_dir()}
    problems = []
    for path, data in entries.items():
        low = path.lower()
        if low.endswith(HOST_BINARY_SUFFIXES) or data[:4] in (b"\x7fELF", b"\xcf\xfa\xed\xfe"):
            problems.append(f"{path}: host binary")
        elif low.endswith((".pyd", ".dll")):
            try:
                if pe.parse(data).machine != pe.IMAGE_FILE_MACHINE_AMD64:
                    problems.append(f"{path}: not an x64 PE file")
            except pe.PEError as exc:
                problems.append(f"{path}: {exc}")
    if problems:
        raise PyxbuildError("cross-built wheel is not a Windows wheel:\n  "
                            + "\n  ".join(problems))
    info_dir = next(p.split("/")[0] for p in entries if p.split("/")[0].endswith(".dist-info"))
    wheel_meta = entries[f"{info_dir}/WHEEL"].decode()
    lines = [ln for ln in wheel_meta.splitlines() if not ln.startswith("Tag:")]
    lines += [f"Tag: {t}" for t in new_tags]
    entries[f"{info_dir}/WHEEL"] = ("\n".join(lines) + "\n").encode()
    record_path = f"{info_dir}/RECORD"
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    for path, data in entries.items():
        if path != record_path:
            writer.writerow([path, _record_hash(data), len(data)])
    writer.writerow([record_path, "", ""])
    entries[record_path] = buf.getvalue().encode()
    tag_str = ".".join(sorted({t.interpreter for t in new_tags})) + "-" + ".".join(
        sorted({t.abi for t in new_tags})) + "-" + platform
    dist = str(name).replace("-", "_")
    build_part = f"-{build[0]}{build[1]}" if build else ""
    out = out_dir / f"{dist}-{version}{build_part}-{tag_str}.whl"
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, data in entries.items():
            zf.writestr(zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0)), data)
    return out
