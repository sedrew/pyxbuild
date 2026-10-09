"""Static checks of a built bundle: missing DLLs, VC++ runtime, path lengths, known issues."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from packaging.utils import canonicalize_name

from . import pe

ERROR = "error"
WARNING = "warning"
INFO = "info"

# DLLs shipped with every supported Windows (10 1809+ / Server 2019+).
# Lower-case. Anything matching api-ms-win-* / ext-ms-win-* is a Windows API set.
SYSTEM_DLLS = frozenset(
    """
advapi32.dll authz.dll avrt.dll bcrypt.dll bcryptprimitives.dll cabinet.dll cfgmgr32.dll
clbcatq.dll combase.dll comctl32.dll comdlg32.dll comppkgsup.dll coremessaging.dll
credui.dll crypt32.dll cryptbase.dll cryptnet.dll cryptsp.dll cryptui.dll d2d1.dll d3d10.dll
d3d10_1.dll d3d11.dll d3d12.dll d3d9.dll d3dcompiler_47.dll dbgeng.dll dbghelp.dll dcomp.dll
ddraw.dll devobj.dll dhcpcsvc.dll dnsapi.dll dsound.dll dwmapi.dll dwmcore.dll dwrite.dll
dxcore.dll dxgi.dll dxva2.dll esent.dll evr.dll firewallapi.dll fltlib.dll fwpuclnt.dll
gdi32.dll gdiplus.dll glu32.dll hid.dll httpapi.dll imagehlp.dll imm32.dll inputhost.dll
iphlpapi.dll kernel32.dll kernelbase.dll ksuser.dll mf.dll mfcore.dll mfplat.dll
mfreadwrite.dll mfuuid.dll mlang.dll mpr.dll msacm32.dll mscms.dll msi.dll msimg32.dll
msvcrt.dll mswsock.dll msxml6.dll ncrypt.dll netapi32.dll normaliz.dll nsi.dll ntdll.dll
ntdsapi.dll ole32.dll oleacc.dll oleaut32.dll opengl32.dll pdh.dll photometadatahandler.dll
powrprof.dll propsys.dll psapi.dll rasapi32.dll rpcrt4.dll rstrtmgr.dll sechost.dll
secur32.dll setupapi.dll shcore.dll shell32.dll shlwapi.dll srvcli.dll sspicli.dll tdh.dll
ucrtbase.dll umpdc.dll urlmon.dll user32.dll userenv.dll usp10.dll uxtheme.dll version.dll
websocket.dll wer.dll wevtapi.dll windows.storage.dll windowscodecs.dll winhttp.dll
wininet.dll winmm.dll winnsi.dll winspool.drv wintrust.dll winusb.dll wlanapi.dll
wldap32.dll wmi.dll ws2_32.dll wsock32.dll wtsapi32.dll xinput1_4.dll xmllite.dll
""".split()  # noqa: SIM905
)

# Visual C++ runtime family: not part of Windows; must ship with the app.
VC_RUNTIME_RE = re.compile(
    r"^(vcruntime140(_1|_threads)?|msvcp140(_1|_2|_atomic_wait|_codecvt_ids)?|"
    r"concrt140|vcomp140|vccorlib140|vcamp140|mfc140u?)\.dll$"
)

# Packages whose behaviour on the target needs a heads-up (see docs/KNOWN_ISSUES.md).
KNOWN_ISSUES: dict[str, tuple[str, str]] = {
    "playwright": (WARNING, "downloads browser binaries at run time (`playwright install`); "
                   "they are not part of the bundle"),
    "pywin32": (INFO, "relies on pywin32.pth and pywin32_system32; works because site is "
                "enabled, no post-install script is run"),
    "pyinstaller": (WARNING, "is a build tool and is not needed inside an application bundle"),
    "pyqt5": (INFO, "Qt platform plugins must be found at run time; smoke-test the GUI"),
    "pyside6": (INFO, "Qt platform plugins must be found at run time; smoke-test the GUI"),
    "pyqt6": (INFO, "Qt platform plugins must be found at run time; smoke-test the GUI"),
    "torch": (WARNING, "needs the VC++ runtime (c10.dll) and has very long paths; "
              "check the DLL report below"),
}

# MAX_PATH minus a typical install prefix such as
# C:\Users\<user>\AppData\Local\Programs\<app>\ (about 60 characters).
PATH_BUDGET = 260 - 60


@dataclass
class Finding:
    level: str
    message: str
    path: str | None = None


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, level: str, message: str, path: str | None = None) -> None:
        self.findings.append(Finding(level, message, path))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == ERROR]

    def format(self) -> str:
        if not self.findings:
            return "no problems found"
        order = {ERROR: 0, WARNING: 1, INFO: 2}
        lines = []
        for f in sorted(self.findings, key=lambda f: (order[f.level], f.path or "", f.message)):
            where = f" [{f.path}]" if f.path else ""
            lines.append(f"{f.level:7} {f.message}{where}")
        return "\n".join(lines)


def is_system_dll(name: str) -> bool:
    low = name.lower()
    return low in SYSTEM_DLLS or low.startswith(("api-ms-win-", "ext-ms-win-"))


def check_bundle(app_dir: Path, runtime_dir: Path, *, machine: int = pe.IMAGE_FILE_MACHINE_AMD64,
                 path_budget: int = PATH_BUDGET) -> Report:
    """Check every PE file in the bundle for unresolved DLL imports and more.

    A DLL import is satisfied, in the order Windows searches with Python's
    ``LOAD_LIBRARY_SEARCH_DEFAULT_DIRS``, by: the importing module's own
    directory, the application (``python.exe``) directory, System32 (the
    known system DLL list), or a directory a package adds at import time with
    ``os.add_dll_directory`` (approximated: any other directory in the bundle,
    reported as info).
    """
    report = Report()
    pe_files: list[Path] = []
    by_name: dict[str, list[Path]] = {}
    for path in sorted(app_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(app_dir).as_posix()
        if len(rel) > path_budget:
            report.add(WARNING, f"path is {len(rel)} characters long; with the install "
                       f"directory it may exceed MAX_PATH (260) on systems without "
                       f"LongPathsEnabled", rel)
        if path.suffix.lower() in (".dll", ".pyd", ".exe"):
            pe_files.append(path)
            by_name.setdefault(path.name.lower(), []).append(path)

    for path in pe_files:
        rel = path.relative_to(app_dir).as_posix()
        try:
            info = pe.parse(path.read_bytes())
        except pe.PEError as exc:
            report.add(WARNING, f"cannot parse PE file: {exc}", rel)
            continue
        if info.machine != machine:
            # .exe files of another architecture are usually launcher templates
            # (pip, setuptools); a .pyd or .dll will fail to load.
            level = {".pyd": ERROR, ".dll": WARNING}.get(path.suffix.lower(), INFO)
            report.add(level, f"PE file for another architecture (machine "
                       f"{info.machine:#06x})", rel)
            continue
        for dll, delayed in [(d, False) for d in info.imports] + [
                (d, True) for d in info.delay_imports]:
            low = dll.lower()
            candidates = by_name.get(low, [])
            if any(c.parent in (path.parent, runtime_dir) for c in candidates):
                continue
            if is_system_dll(low):
                continue
            kind = "delay-loaded DLL" if delayed else "DLL"
            if candidates:
                where = ", ".join(sorted({c.parent.relative_to(app_dir).as_posix()
                                          for c in candidates}))
                report.add(INFO, f"{kind} {dll} found only in {where}; the package must "
                           f"add that directory with os.add_dll_directory", rel)
            elif VC_RUNTIME_RE.match(low):
                report.add(WARNING if delayed else ERROR,
                           f"needs Visual C++ runtime {dll}, which is not in the bundle and "
                           f"not part of Windows", rel)
            else:
                report.add(WARNING if delayed else ERROR,
                           f"{kind} {dll} not found in the bundle and not a known Windows "
                           f"system DLL", rel)
    _check_known_issues(runtime_dir, report)
    return report


def _check_known_issues(runtime_dir: Path, report: Report) -> None:
    site = runtime_dir / "Lib" / "site-packages"
    for meta in sorted(site.glob("*.dist-info/METADATA")):
        name = None
        for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Name:"):
                name = canonicalize_name(line.split(":", 1)[1].strip())
                break
            if not line:
                break
        if name and name in KNOWN_ISSUES:
            level, message = KNOWN_ISSUES[name]
            report.add(level, f"{name} {message}")
