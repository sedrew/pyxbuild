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

# DLLs shipped with every supported Windows (10 1903+ / Server 2022+; icu*.dll since 1903).
# Lower-case. Anything matching api-ms-win-* / ext-ms-win-* is a Windows API set.
SYSTEM_DLLS = frozenset(
    """
aclui.dll activeds.dll adsldpc.dll advapi32.dll audioses.dll authz.dll avicap32.dll
avifil32.dll avrt.dll bcrypt.dll bcryptprimitives.dll bluetoothapis.dll bthprops.cpl
cabinet.dll cfgmgr32.dll clbcatq.dll cldapi.dll clusapi.dll combase.dll comctl32.dll
comdlg32.dll comppkgsup.dll coremessaging.dll credui.dll crypt32.dll cryptbase.dll
cryptnet.dll cryptsp.dll cryptui.dll cscapi.dll d2d1.dll d3d10.dll d3d10_1.dll d3d11.dll
d3d11on12.dll d3d12.dll d3d9.dll d3dcompiler_43.dll d3dcompiler_47.dll davclnt.dll
dbgcore.dll dbgeng.dll dbghelp.dll dcomp.dll ddraw.dll devobj.dll dhcpcsvc.dll dhcpcsvc6.dll
dinput8.dll dnsapi.dll dpapi.dll dsound.dll dsrole.dll dwmapi.dll dwmcore.dll dwrite.dll
dxcompiler.dll dxcore.dll dxgi.dll dxva2.dll esent.dll evr.dll faultrep.dll firewallapi.dll
fltlib.dll fwbase.dll fwpuclnt.dll gdi32.dll gdiplus.dll glu32.dll hid.dll httpapi.dll
icmp.dll icu.dll icuin.dll icuuc.dll imagehlp.dll imm32.dll inputhost.dll iphlpapi.dll
kernel32.dll kernelbase.dll ksuser.dll ktmw32.dll loadperf.dll locationapi.dll logoncli.dll
lz32.dll mapi32.dll mf.dll mfcore.dll mfplat.dll mfreadwrite.dll mfsensorgroup.dll
mfsrcsnk.dll mfuuid.dll mlang.dll mmdevapi.dll mpr.dll msacm32.dll mscms.dll msctf.dll
msi.dll msimg32.dll msvcrt.dll msvfw32.dll mswsock.dll msxml6.dll ncrypt.dll netapi32.dll
netutils.dll normaliz.dll nsi.dll ntdll.dll ntdsapi.dll ntmarta.dll odbc32.dll odbccp32.dll
ole32.dll oleacc.dll oleaut32.dll oledlg.dll opengl32.dll pdh.dll photometadatahandler.dll
powrprof.dll profapi.dll propsys.dll psapi.dll query.dll rasapi32.dll resutils.dll
rpcrt4.dll rstrtmgr.dll samcli.dll sechost.dll secur32.dll sensorsapi.dll setupapi.dll
sfc.dll sfc_os.dll shcore.dll shell32.dll shfolder.dll shlwapi.dll srvcli.dll sspicli.dll
tapi32.dll tdh.dll traffic.dll twinapi.dll ucrtbase.dll uiautomationcore.dll umpdc.dll
urlmon.dll user32.dll userenv.dll usp10.dll uxtheme.dll version.dll virtdisk.dll vssapi.dll
wbemuuid.dll websocket.dll wer.dll wevtapi.dll windows.storage.dll windowscodecs.dll
winhttp.dll wininet.dll winmm.dll winnsi.dll winrt.dll winscard.dll winspool.drv
wintrust.dll wintypes.dll winusb.dll wkscli.dll wlanapi.dll wldap32.dll wmi.dll wmvcore.dll
ws2_32.dll wsock32.dll wtsapi32.dll xaudio2_9.dll xinput1_4.dll xinput9_1_0.dll xmllite.dll
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
              "check the DLL report below; torch.compile also needs a C compiler and "
              "keep-dev-files = true"),
    "triton-windows": (WARNING, "compiles kernels at run time: needs a C compiler, "
                       "keep-dev-files = true (Python.h, python3XY.lib)"),
    "numba": (INFO, "cache=True needs a writable cache; set NUMBA_CACHE_DIR through "
              "[tool.pyxbuild.env], e.g. \"{data_dir}\\\\numba\""),
    "comtypes": (WARNING, "generates modules into comtypes/gen at run time; a read-only "
                 "install folder needs comtypes.client.gen_dir pointed elsewhere"),
    "nltk": (WARNING, "corpora/models are downloaded at run time (nltk.download); bundle "
             "them and set NLTK_DATA through [tool.pyxbuild.env]"),
    "spacy": (WARNING, "models are separate packages (spacy download uses pip, which is not "
              "in the bundle); add the model wheel to the dependencies or find-links"),
    "transformers": (WARNING, "models are downloaded into HF_HOME at run time; set HF_HOME "
                     "through [tool.pyxbuild.env] or ship the model"),
    "huggingface-hub": (INFO, "downloads into HF_HOME at run time; set it through "
                        "[tool.pyxbuild.env] if the app must run offline"),
    "easyocr": (WARNING, "downloads its models at run time into ~/.EasyOCR"),
    "ansible-core": (WARNING, "does not support Windows as a control node: the CLI fails "
                     "at start-up (os.get_blocking, fcntl, pwd); run it under WSL instead"),
    "cffi": (INFO, "API-mode modules compiled at run time (ffi.compile) need a C compiler "
             "and keep-dev-files = true; prebuilt _cffi modules are fine"),
}

# OpenMP runtimes: two in one process -> "OMP: Error #15" or crashes
OPENMP_DLL_RE = re.compile(r"^(libiomp5md|vcomp140|libomp(140)?(\.x86_64)?|libgomp-1)\.dll$")

# Without LongPathsEnabled a full path has at most MAX_PATH - 1 = 259
# characters, and a directory must leave room for an 8.3 name (CreateDirectoryW:
# MAX_PATH - 12). A typical install folder such as
# C:\Users\<user>\AppData\Local\Programs\<app>\ takes about 60 characters.
MAX_PATH_CHARS = 259
DIR_RESERVE = 12
TYPICAL_PREFIX = 60

# scripts that a package expects someone to run after "pip install"
POSTINSTALL_RE = re.compile(r"post[-_]?install", re.IGNORECASE)


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

    def format(self, max_paths: int = 3) -> str:
        """One line per distinct problem, listing a few of the files it affects."""
        if not self.findings:
            return "no problems found"
        order = {ERROR: 0, WARNING: 1, INFO: 2}
        groups: dict[tuple[str, str], list[str]] = {}
        for f in self.findings:
            groups.setdefault((f.level, f.message), []).append(f.path or "")
        lines = []
        for (level, message), paths in sorted(groups.items(),
                                              key=lambda kv: (order[kv[0][0]], kv[0][1])):
            paths = sorted(p for p in paths if p)
            where = ""
            if paths:
                shown = ", ".join(paths[:max_paths])
                more = f" and {len(paths) - max_paths} more" if len(paths) > max_paths else ""
                where = f" [{shown}{more}]"
            lines.append(f"{level:7} {message}{where}")
        return "\n".join(lines)


def is_system_dll(name: str) -> bool:
    low = name.lower()
    return low in SYSTEM_DLLS or low.startswith(("api-ms-win-", "ext-ms-win-"))


def _check_path_lengths(report: Report, need: int, rel: str, typical_prefix: int) -> None:
    """How long may the install folder be before some path exceeds MAX_PATH?"""
    room = MAX_PATH_CHARS - need  # characters left for "C:\\...\\<app folder>\\"
    if room >= typical_prefix:
        return
    advice = ("enable LongPathsEnabled on the target, shorten the path with "
              "[tool.pyxbuild] exclude, or ship an installer that picks a short folder; "
              "Explorer's zip extraction ignores LongPathsEnabled, and extracting into "
              "Downloads repeats the folder name")
    if room < len("C:\\"):
        report.add(ERROR, f"a path in the bundle needs {need} characters, more than MAX_PATH "
                   f"allows even directly under C:\\; it cannot be loaded on Windows without "
                   f"LongPathsEnabled; {advice}", rel)
    else:
        report.add(WARNING, f"longest path needs {need} characters: without LongPathsEnabled "
                   f"the application folder (with drive and trailing backslash) may be at "
                   f"most {room} characters long (MAX_PATH is 260); {advice}", rel)


def check_bundle(app_dir: Path, runtime_dir: Path, *, machine: int = pe.IMAGE_FILE_MACHINE_AMD64,
                 typical_prefix: int = TYPICAL_PREFIX) -> Report:
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
    worst_need, worst_rel = 0, ""
    scripts_dir = runtime_dir / "Scripts"
    for path in sorted(app_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(app_dir).as_posix()
        parent = rel.rpartition("/")[0]
        need = max(len(rel), len(parent) + DIR_RESERVE if parent else 0)
        if need > worst_need:
            worst_need, worst_rel = need, rel
        if path.parent == scripts_dir and POSTINSTALL_RE.search(path.name):
            report.add(INFO, f"{path.name} is a post-install script that neither pip nor "
                       f"pyxbuild runs; if the application needs what it sets up (COM "
                       f"registration, shortcuts, downloads), do that on the target", rel)
        if path.suffix.lower() in (".dll", ".pyd", ".exe"):
            pe_files.append(path)
            by_name.setdefault(path.name.lower(), []).append(path)

    _check_path_lengths(report, worst_need, worst_rel, typical_prefix)

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
            # A .pyd that cannot resolve an import fails on `import`; a .dll or
            # .exe may be an optional plugin that is never loaded.
            level = ERROR if path.suffix.lower() == ".pyd" and not delayed else WARNING
            if candidates:
                where = ", ".join(sorted({c.parent.relative_to(app_dir).as_posix()
                                          for c in candidates}))
                report.add(INFO, f"{kind} {dll} found only in {where}; the package must "
                           f"add that directory with os.add_dll_directory", rel)
            elif VC_RUNTIME_RE.match(low):
                report.add(level, f"needs Visual C++ runtime {dll}, which is not in the bundle "
                           f"and not part of Windows (add 'msvc-runtime' to the dependencies "
                           f"to ship it)", rel)
            else:
                report.add(level, f"{kind} {dll} not found in the bundle and not a known "
                           f"Windows system DLL", rel)
    _check_openmp(by_name, app_dir, report)
    _check_known_issues(runtime_dir, report)
    _check_metadata_markers(runtime_dir, report)
    return report


def _check_openmp(by_name: dict[str, list[Path]], app_dir: Path, report: Report) -> None:
    found = sorted({p.relative_to(app_dir).as_posix() for name, paths in by_name.items()
                    if OPENMP_DLL_RE.match(name) for p in paths})
    kinds = {Path(p).name.lower() for p in found}
    if len(kinds) > 1:
        report.add(WARNING, "more than one OpenMP runtime in the bundle (" + ", ".join(
            sorted(kinds)) + "); loading two into one process fails with 'OMP: Error #15' "
            "or crashes; import order may matter (KMP_DUPLICATE_LIB_OK is a last resort)")


def _check_metadata_markers(runtime_dir: Path, report: Report) -> None:
    """Requires-Dist markers pyxbuild cannot evaluate for the target."""
    site = runtime_dir / "Lib" / "site-packages"
    for meta in sorted(site.glob("*.dist-info/METADATA")):
        for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line:
                break
            if line.startswith("Requires-Dist:") and (
                    "platform_release" in line or "platform_version" in line):
                report.add(WARNING, f"{line.split(':', 1)[1].strip()}: platform_release / "
                           f"platform_version are unknown when resolving on another OS and "
                           f"evaluate as empty strings", meta.parent.name)


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
