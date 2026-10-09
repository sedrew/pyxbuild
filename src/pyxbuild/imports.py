"""Static import check of the project's own code against the built bundle.

pyxbuild installs whole wheels, so "hidden imports" inside dependencies are
not a problem. What can still break at run time is the project's own code:

* importing a module no installed distribution provides (a dependency that
  is used but not declared, or declared only under an extra / a marker);
* importing a POSIX-only stdlib module (``fcntl``, ``termios``, ...) that
  does not exist on Windows;
* ``importlib.import_module("name")`` / ``__import__("name")`` with a
  constant name that is missing.

An import inside ``try: ... except ImportError``, under an
``if sys.platform/os.name/platform.system()`` check, or inside a function is
reported as a warning (an optional or lazy feature), a plain module-level
import as an error.
"""

from __future__ import annotations

import ast
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import doctor

# Stdlib modules missing from CPython on Windows.
POSIX_ONLY = frozenset({
    "fcntl", "termios", "tty", "pty", "pwd", "grp", "resource", "posix", "readline",
    "crypt", "spwd", "syslog", "nis", "ossaudiodev", "_posixsubprocess", "curses",
    "_curses", "_curses_panel", "_posixshmem",
})

# Modules compiled into python3XY.dll on Windows (not files in Lib/ or DLLs/).
WINDOWS_BUILTINS = frozenset({
    "nt", "winreg", "msvcrt", "_winapi", "_io", "_thread", "_signal", "_codecs", "_sre",
    "_abc", "_ast", "_bisect", "_blake2", "_codecs_cn", "_codecs_hk", "_codecs_iso2022",
    "_codecs_jp", "_codecs_kr", "_codecs_tw", "_collections", "_contextvars", "_csv",
    "_datetime", "_functools", "_heapq", "_imp", "_json", "_locale", "_lsprof", "_md5",
    "_multibytecodec", "_opcode", "_operator", "_pickle", "_random", "_sha1", "_sha2",
    "_sha256", "_sha3", "_sha512", "_stat", "_statistics", "_string", "_struct",
    "_symtable", "_tokenize", "_tracemalloc", "_typing", "_warnings", "_weakref",
    "_xxsubinterpreters",
    "_interpreters", "_interpqueues", "_interpchannels", "array", "atexit", "audioop",
    "binascii", "builtins", "cmath", "errno", "faulthandler", "gc", "itertools", "marshal",
    "math", "mmap", "sys", "time", "xxsubtype", "zlib", "_suggestions", "_sysconfig",
    "_frozen_importlib", "_frozen_importlib_external",
})

# import name -> distribution to add (common mismatches)
DIST_HINTS = {
    "yaml": "PyYAML", "PIL": "Pillow", "cv2": "opencv-python-headless", "sklearn":
    "scikit-learn", "bs4": "beautifulsoup4", "dateutil": "python-dateutil", "dotenv":
    "python-dotenv", "jwt": "PyJWT", "magic": "python-magic", "serial": "pyserial",
    "usb": "pyusb", "Crypto": "pycryptodome", "OpenSSL": "pyOpenSSL", "win32api":
    "pywin32", "win32con": "pywin32", "pythoncom": "pywin32", "pywintypes": "pywin32",
    "fitz": "PyMuPDF", "docx": "python-docx", "pptx": "python-pptx", "attr": "attrs",
    "google.protobuf": "protobuf", "zmq": "pyzmq", "git": "GitPython", "skimage":
    "scikit-image", "tomli_w": "tomli-w", "pkg_resources": "setuptools",
}

PLATFORM_GUARDS = ("sys.platform", "os.name", "platform.system", "sys.platform.startswith")


@dataclass
class ImportUse:
    module: str
    file: str
    line: int
    guarded: bool
    lazy: bool
    dynamic: bool


def _is_platform_test(node: ast.AST) -> bool:
    text = ast.unparse(node)
    return any(g in text for g in PLATFORM_GUARDS)


class _Visitor(ast.NodeVisitor):
    def __init__(self, file: str) -> None:
        self.file = file
        self.uses: list[ImportUse] = []
        self._guard = 0
        self._func = 0

    def _add(self, module: str, node: ast.AST, dynamic: bool = False) -> None:
        self.uses.append(ImportUse(module, self.file, getattr(node, "lineno", 0),
                                   self._guard > 0, self._func > 0, dynamic))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._add(alias.name, node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and node.module:
            self._add(node.module, node)

    def visit_Try(self, node: ast.Try) -> None:
        catches = any(
            h.type is None or any(n in ast.unparse(h.type) for n in
                                  ("ImportError", "ModuleNotFoundError", "Exception"))
            for h in node.handlers)
        if catches:
            self._guard += 1
            for stmt in node.body:
                self.visit(stmt)
            self._guard -= 1
            for part in (node.handlers, node.orelse, node.finalbody):
                for stmt in part:
                    self.visit(stmt)
        else:
            self.generic_visit(node)

    visit_TryStar = visit_Try

    def visit_If(self, node: ast.If) -> None:
        if _is_platform_test(node.test) or "TYPE_CHECKING" in ast.unparse(node.test):
            self._guard += 1
            self.generic_visit(node)
            self._guard -= 1
        else:
            self.generic_visit(node)

    def _visit_func(self, node: ast.AST) -> None:
        self._func += 1
        self.generic_visit(node)
        self._func -= 1

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _visit_func

    def visit_Call(self, node: ast.Call) -> None:
        name = ast.unparse(node.func)
        if name in ("importlib.import_module", "import_module", "__import__") and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                    and not arg.value.startswith("."):
                self._add(arg.value, node, dynamic=True)
        self.generic_visit(node)


def scan_source(source: str, file: str) -> list[ImportUse]:
    try:
        tree = ast.parse(source, filename=file)
    except SyntaxError:
        return []
    visitor = _Visitor(file)
    visitor.visit(tree)
    return visitor.uses


def available_modules(runtime_dir: Path) -> set[str]:
    """Top-level importable names in the bundle (stdlib + site-packages)."""
    names = set(WINDOWS_BUILTINS) | set(sys.builtin_module_names) - POSIX_ONLY
    lib = runtime_dir / "Lib"
    site = lib / "site-packages"
    roots = [lib, site, runtime_dir / "DLLs"]
    # .pth files add directories to sys.path (pywin32: win32, win32/lib, pythonwin)
    for pth in sorted(site.glob("*.pth")) if site.is_dir() else []:
        for line in pth.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith(("#", "import ", "import\t")):
                extra = (site / line.replace("\\", "/")).resolve()
                if extra.is_dir() and extra.is_relative_to(runtime_dir.resolve()):
                    roots.append(extra)
    for root in roots:
        if not root.is_dir():
            continue
        for item in root.iterdir():
            if item == site:
                continue
            if item.is_dir():
                if item.name.endswith((".dist-info", ".egg-info", ".libs", ".data")) or \
                        item.name in ("__pycache__",):
                    continue
                names.add(item.name)  # packages and namespace packages
            elif item.suffix in (".py", ".pyd", ".pyc"):
                names.add(item.name.split(".")[0])
    return names


def project_sources(wheel: Path) -> dict[str, str]:
    out = {}
    with zipfile.ZipFile(wheel) as zf:
        for name in zf.namelist():
            if name.endswith(".py") and ".dist-info/" not in name:
                out[name] = zf.read(name).decode("utf-8", errors="replace")
    return out


def check_imports(runtime_dir: Path, project_wheel: Path, report: doctor.Report) -> None:
    available = available_modules(runtime_dir)
    sources = project_sources(project_wheel)
    own = {name.split("/")[0].removesuffix(".py") for name in sources}
    for file, text in sorted(sources.items()):
        for use in scan_source(text, file):
            top = use.module.split(".")[0]
            if top in own or top == "__future__":
                continue
            where = f"{use.file}:{use.line}"
            soft = use.guarded or use.lazy
            how = "dynamic import of" if use.dynamic else "import"
            if top in POSIX_ONLY:
                if not use.guarded:
                    report.add(doctor.WARNING if use.lazy else doctor.ERROR,
                               f"{how} {top!r}: this stdlib module does not exist on Windows "
                               f"(guard it with sys.platform)", where)
                continue
            if top in available:
                continue
            dist = DIST_HINTS.get(use.module) or DIST_HINTS.get(top)
            hint = f"; add {dist!r} to the dependencies" if dist else (
                "; add the distribution that provides it to the dependencies")
            if soft:
                report.add(doctor.WARNING, f"optional {how} {top!r} is not in the bundle "
                           f"(feature unavailable on Windows){hint}", where)
            else:
                report.add(doctor.ERROR, f"{how} {top!r} is not in the bundle: the app fails "
                           f"when this module is imported{hint}", where)
