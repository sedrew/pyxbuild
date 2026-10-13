"""Install wheels into the bundled runtime without running any of their code.

Wheels are installed with the PyPA ``installer`` library into the Windows
``nt`` sysconfig scheme of the bundled interpreter, so the result matches
``pip install`` on the target: ``*.dist-info`` with a full ``RECORD``,
``INSTALLER`` set to ``pyxbuild``, ``.data/scripts`` and ``.data/data``
honoured, ``.pth`` files left for ``site`` to process at start-up.
"""

from __future__ import annotations

import csv
import io
import os
import posixpath
import re
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from installer import install
from installer.destinations import SchemeDictionaryDestination
from installer.records import Hash, RecordEntry
from installer.sources import WheelFile
from installer.utils import Scheme, construct_record_file, copyfileobj_with_hashing

from .errors import PyxbuildError

INSTALLER_NAME = b"pyxbuild"


def scheme(target, runtime_dir: Path, dist_name: str, python: str) -> dict[str, str]:
    """``sysconfig`` scheme of the target interpreter living in ``runtime_dir``."""
    if target.is_windows:
        return nt_scheme(runtime_dir, dist_name)
    site = str(runtime_dir / target.site_packages(python))
    return {
        "purelib": site,
        "platlib": site,
        "headers": str(runtime_dir / "include" / f"python{python}" / dist_name),
        "scripts": str(runtime_dir / "bin"),
        "data": str(runtime_dir),
    }


POSIX_SCRIPT_HEAD = Path(__file__).with_name("_boot").joinpath("posix_script_head.txt")
POSIX_SHEBANG_MARK = "pyxbuild-relocatable"


def posix_script_head(python: str) -> str:
    """Relocatable POSIX scripts (like ``uv venv --relocatable``): the shell runs
    the first lines, Python sees a string; the interpreter is found next to the
    script wherever the application folder is."""
    return POSIX_SCRIPT_HEAD.read_text(encoding="utf-8").replace("{python}", python)


def nt_scheme(runtime_dir: Path, dist_name: str) -> dict[str, str]:
    """``sysconfig`` ``nt`` scheme for an interpreter living in ``runtime_dir``."""
    return {
        "purelib": str(runtime_dir / "Lib" / "site-packages"),
        "platlib": str(runtime_dir / "Lib" / "site-packages"),
        "headers": str(runtime_dir / "Include" / dist_name),
        "scripts": str(runtime_dir / "Scripts"),
        "data": str(runtime_dir),
    }


# Windows file name rules (CreateFileW without the \\?\ prefix)
_INVALID_CHARS = set('<>:"|?*') | {chr(c) for c in range(32)}
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{dev}{n}" for dev in ("COM", "LPT") for n in "123456789\u00b9\u00b2\u00b3"}


def windows_name_problem(component: str) -> str | None:
    """Why ``component`` cannot be a file or directory name on Windows, if it cannot."""
    bad = sorted({c for c in component if c in _INVALID_CHARS})
    if bad:
        return "contains " + ", ".join(repr(c) for c in bad) + ", not allowed in Windows names"
    if component.endswith((" ", ".")):
        return "ends with a space or a dot, which Windows strips"
    if component.split(".", 1)[0].rstrip(" ").upper() in _RESERVED_NAMES:
        return "is a reserved device name on Windows"
    return None


def _win_key(name: str) -> str:
    """Case-insensitive key like NTFS's: simple per-character upper case (no ß -> SS)."""
    return "".join(u if len(u := c.upper()) == 1 else c for c in name)


class _NullWriter:
    def write(self, data: bytes) -> int:
        return len(data)


@dataclass
class CaseMerge:
    wheel: str
    wanted: str     # path as written in the wheel, relative to the runtime
    existing: str   # what is already there (another wheel's spelling or the runtime's)
    owner: str      # who created ``existing``


class InstallState:
    """What is installed so far, seen the way Windows sees the folder.

    Windows file names are case-insensitive: ``Crypto/`` and ``crypto/`` are
    one folder, and the first spelling wins. Linux (and case-sensitive APFS)
    would create two, so a bundle built there differs from one built on
    macOS/Windows and from what pip produces on the target. Every path is
    therefore mapped onto the spelling that already exists, on every host.
    Shared by all wheels of one application.
    """

    RUNTIME = "the Python runtime"

    def __init__(self, runtime_dir: Path, excluder: Excluder | None = None,
                 target=None, python: str = "") -> None:
        from .targets import WIN_AMD64

        self.runtime_dir = runtime_dir
        self.excluder = excluder
        self.target = target or WIN_AMD64
        self.python = python
        # Windows: case-insensitive names; Linux: names are taken as they are
        self._key = _win_key if self.target.is_windows else (lambda name: name)
        self.case_merges: list[CaseMerge] = []
        self.excluded: list[str] = []
        self._names: dict[str, dict[str, str]] = {}   # dir (canonical) -> key -> name
        self._owner: dict[str, str] = {}              # canonical path -> wheel

    def _listing(self, rel_dir: str) -> dict[str, str]:
        names = self._names.get(rel_dir)
        if names is None:
            d = self.runtime_dir / rel_dir if rel_dir else self.runtime_dir
            names = {self._key(n): n for n in sorted(os.listdir(d))} if d.is_dir() else {}
            self._names[rel_dir] = names
        return names

    def canonical(self, rel: str, wheel: str) -> str:
        """``rel`` (relative to the runtime) spelled as it already exists."""
        out: list[str] = []
        for part in rel.split("/"):
            parent = "/".join(out)
            names = self._listing(parent)
            key = self._key(part)
            existing = names.get(key)
            if existing is None:
                names[key] = existing = part
                self._owner[f"{parent}/{part}" if parent else part] = wheel
            elif existing != part:
                where = f"{parent}/{existing}" if parent else existing
                self.case_merges.append(CaseMerge(
                    wheel, f"{parent}/{part}" if parent else part, where,
                    self._owner.get(where, self.RUNTIME)))
            out.append(existing)
        return "/".join(out)

    def owner(self, canonical: str) -> str:
        return self._owner.get(canonical, self.RUNTIME)

    def add_findings(self, report) -> None:
        """Report case-only clashes (doctor findings)."""
        from . import doctor

        seen: set[tuple[str, str]] = set()
        for m in self.case_merges:
            if (m.wanted, m.existing) in seen:
                continue
            seen.add((m.wanted, m.existing))
            if m.owner not in (m.wheel, self.RUNTIME):
                report.add(doctor.WARNING, f"{m.wheel} installs {m.wanted!r} but {m.owner} "
                           f"already installed {m.existing!r}: names differ only by case, so "
                           f"on Windows they are one folder/file and imports of one spelling "
                           f"fail; keep only one of the two packages", m.wanted)
            else:
                report.add(doctor.INFO, f"{m.wheel} writes {m.wanted!r}, which Windows "
                           f"stores as the existing {m.existing!r} (names differ only by "
                           f"case)", m.wanted)


class _Destination(SchemeDictionaryDestination):
    """Like the base class, but the result never depends on the host OS.

    ``installer`` writes absolute paths for out-of-scheme files when it runs
    on Windows; pip writes ``../../Scripts/x.exe``. We always write the
    relative POSIX form so the bundle is byte-identical on every host. Paths
    are laid out with Windows' case-insensitive rules (``InstallState``) and
    names Windows cannot store are refused.
    """

    target = "win_amd64"

    def __init__(self, *args, wheel: str, state: InstallState, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.wheel = wheel
        self.state = state
        self.skipped: set[str] = set()

    def write_to_fs(self, scheme: Scheme, path: str, stream, is_executable: bool):
        # Defence in depth (installer >= 1.0 checks this too): a wheel entry
        # must stay inside its scheme directory.
        parts = PurePosixPath(path.replace("\\", "/")).parts
        # A drive letter escapes the folder; any other ':' (an NTFS stream,
        # "file:stream") is refused below as a name Windows cannot store.
        if (not parts or ".." in parts or path.startswith(("/", "\\"))
                or re.match(r"[A-Za-z]:", path)):
            raise PyxbuildError(f"refusing wheel entry outside the install folder: {path!r}")
        in_site = scheme in ("purelib", "platlib")
        metadata = ".dist-info/" in path and path.count("/") == 1
        excluder = self.state.excluder
        if in_site and not metadata and excluder and excluder.matches(path):
            # Not written at all (also lets an app drop files Windows cannot store).
            self.skipped.add(path)
            self.state.excluded.append(path)
            return RecordEntry(path, None, None)
        # before touching the file system: on a Windows host "a:b" would be
        # taken as a drive or an NTFS stream
        for part in parts if self.state.target.is_windows else ():
            problem = windows_name_problem(part)
            if problem:
                hint = (f'if the application does not need it, add "{path}" to '
                        f"[tool.pyxbuild] exclude" if in_site else None)
                raise PyxbuildError(f"{self.wheel}: cannot install {path!r} for Windows: "
                                    f"{part!r} {problem}", hint=hint)
        root = Path(self.scheme_dict[scheme]).resolve()
        if not (root / path).resolve().is_relative_to(root):
            raise PyxbuildError(f"refusing wheel entry outside the install folder: {path!r}")

        # Relative to the runtime as the wheel spells it. Not via resolve():
        # on Windows that already returns the on-disk spelling of existing
        # folders, and the case clash would go unreported there.
        runtime = self.state.runtime_dir
        scheme_rel = Path(os.path.relpath(self.scheme_dict[scheme], runtime)).as_posix()
        rel = posixpath.normpath(posixpath.join(scheme_rel, "/".join(parts)))
        canonical = self.state.canonical(rel, self.wheel)
        dest = runtime / canonical
        if not self.state.target.is_windows and dest.exists():
            clash = _case_clash(runtime, canonical)
            if clash:
                raise PyxbuildError(
                    f"{self.wheel}: {canonical!r} differs only by case from the installed "
                    f"{clash!r}; this build host's file system ignores case, so the "
                    f"{self.state.target.name} bundle cannot hold both",
                    hint=f"build {self.state.target.name} on Linux (a case-sensitive file "
                         "system), or keep only one of the two packages")
        if dest.exists():
            # Several wheels may ship the same file: the ``__init__.py`` of a
            # pkgutil-style namespace package (kivy_deps.sdl2/.glew/.angle,
            # old zope/google packages). pip lets the last one win; identical
            # bytes are harmless, different bytes mean two packages fight
            # over one module (opencv-python + opencv-python-headless).
            data = stream.read()
            if not dest.is_file() or dest.read_bytes() != data:
                owner = self.state.owner(canonical)
                case = (f" (the wheel's {rel!r} differs only by case, and Windows names are "
                        f"case-insensitive)" if rel != canonical else "")
                raise FileExistsError(f"{canonical} (installed by {owner}) with different "
                                      f"content{case}")
            hash_, size = copyfileobj_with_hashing(io.BytesIO(data), _NullWriter(),
                                                   self.hash_algorithm)
        else:
            if scheme == "scripts" and not self.state.target.is_windows:
                stream = _relocatable_script(stream, self.state.python)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                hash_, size = copyfileobj_with_hashing(stream, f, self.hash_algorithm)
            if is_executable:
                dest.chmod(dest.stat().st_mode | 0o111)
        # RECORD keeps the wheel's spelling, as pip on Windows writes it
        return RecordEntry(path, Hash(self.hash_algorithm, hash_), size)

    def write_script(self, name: str, module: str, attr: str, section: str) -> RecordEntry:
        """Console/GUI scripts of installed packages use pyxbuild's own launcher.

        The distlib launchers ``installer`` would write need an absolute
        interpreter path; ours finds ``..\\python.exe`` relative to itself,
        so ``runtime\\Scripts\\*.exe`` keep working wherever the app is unpacked.
        """
        from .config import _SCRIPT_NAME_RE, EntryPoint
        from .launcher import build_launcher

        if not _SCRIPT_NAME_RE.match(name):
            raise PyxbuildError(f"invalid console script name {name!r} in a wheel")

        entry = EntryPoint(name=name, module=module, attr=attr, gui=section == "gui")
        if not self.state.target.is_windows:
            body = (posix_script_head(self.state.python)
                    + f"import sys\nfrom {module} import {attr.split('.')[0]}\n"
                    + f"sys.exit({attr}())\n")
            with io.BytesIO(body.encode()) as stream:
                return self.write_to_fs(Scheme("scripts"), name, stream, is_executable=True)
        data = build_launcher(entry, self.target, python_rel="..")
        with io.BytesIO(data) as stream:
            return self.write_to_fs(Scheme("scripts"), f"{name}.exe", stream, is_executable=True)

    def finalize_installation(self, scheme: Scheme, record_file_path: str,
                              records: Iterable[tuple[Scheme, RecordEntry]]) -> None:
        def prefix_for_scheme(file_scheme: str) -> str | None:
            if file_scheme == scheme:
                return None
            rel = os.path.relpath(self.scheme_dict[file_scheme], start=self.scheme_dict[scheme])
            return rel.replace(os.sep, "/") + "/"

        record_list = [(s, r) for s, r in records
                       if not (r.path in self.skipped and r.hash_ is None)]
        with construct_record_file(record_list, prefix_for_scheme) as stream:
            self.write_to_fs(scheme, record_file_path, stream, is_executable=False)


def _case_clash(root: Path, rel: str) -> str | None:
    """The on-disk spelling of ``rel`` if it differs from ``rel`` only by case
    (a case-insensitive build host merged two names), else None."""
    current = root
    found = []
    for part in rel.split("/"):
        names = os.listdir(current)
        if part not in names:
            match = next((n for n in names if n.lower() == part.lower()), None)
            if match is None:
                return None
            found.append(match)
            return "/".join(found + rel.split("/")[len(found):])
        found.append(part)
        current = current / part
    return None


def _relocatable_script(stream, python: str):
    """``#!python`` scripts of ``.data/scripts``: point them at the bundle's interpreter."""
    data = stream.read()
    first, _, rest = data.partition(b"\n")
    if first.startswith(b"#!") and POSIX_SHEBANG_MARK.encode() in first:
        data = posix_script_head(python).encode() + rest
    return io.BytesIO(data)


def install_wheel(wheel_path: Path, runtime_dir: Path,
                  state: InstallState | None = None) -> None:
    """Install one wheel; pass the same ``state`` for every wheel of an application."""
    from packaging.utils import parse_wheel_filename

    name = parse_wheel_filename(wheel_path.name)[0]
    state = state if state is not None else InstallState(runtime_dir)
    windows = state.target.is_windows
    destination = _Destination(
        scheme(state.target, runtime_dir, str(name), state.python),
        interpreter="python.exe" if windows else POSIX_SHEBANG_MARK,
        script_kind="win-amd64" if windows else "posix",
        bytecode_optimization_levels=(),
        wheel=wheel_path.name,
        state=state,
    )
    try:
        with warnings.catch_warnings():
            # installer skips bytecode shipped inside wheels (PySide6 does this)
            # and warns per file; Python regenerates it, so stay quiet.
            warnings.filterwarnings("ignore", message="Skip installing .*__pycache__")
            with WheelFile.open(wheel_path) as source:
                install(source, destination, {"INSTALLER": INSTALLER_NAME})
    except PyxbuildError:
        raise
    except FileExistsError as exc:
        raise PyxbuildError(
            f"{wheel_path.name} overwrites a file that is already installed: {exc}",
            hint="two dependencies provide the same file; keep only one of them",
        ) from None
    except Exception as exc:  # installer raises plain exceptions for broken wheels
        raise PyxbuildError(f"cannot install {wheel_path.name}: {exc}") from exc


# --- exclude patterns -------------------------------------------------------

def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """gitignore-like glob: ``*`` stays inside a path segment, ``**`` crosses them.

    A pattern without ``/`` (other than a trailing one) matches at any depth;
    a pattern with ``/`` is anchored at the root it is applied to.
    """
    pat = pattern.strip()
    dir_only = pat.endswith("/")
    pat = pat.rstrip("/")
    anchored = "/" in pat
    pat = pat.lstrip("/")
    regex = ""
    i = 0
    while i < len(pat):
        if pat.startswith("**/", i):
            regex += "(?:.*/)?"
            i += 3
        elif pat.startswith("**", i):
            regex += ".*"
            i += 2
        else:
            c = pat[i]
            end = pat.find("]", i + 1) if c == "[" else -1
            if c == "*":
                regex += "[^/]*"
            elif c == "?":
                regex += "[^/]"
            elif end > i + 1:
                body = pat[i + 1:end]
                body = body.replace("\\", "\\\\")
                regex += "[^" + body[1:] + "]" if body[0] == "!" else "[" + body + "]"
                i = end
            else:
                regex += re.escape(c)
            i += 1
    prefix = "" if anchored else "(?:.*/)?"
    suffix = "/.*" if dir_only else "(?:/.*)?"
    return re.compile(f"^{prefix}{regex}{suffix}$", re.DOTALL)


class Excluder:
    def __init__(self, patterns: Iterable[str]) -> None:
        self.patterns = [p for p in patterns if p.strip()]
        self._compiled = [_compile_pattern(p) for p in self.patterns]

    def __bool__(self) -> bool:
        return bool(self._compiled)

    def matches(self, rel_posix: str) -> bool:
        return any(rx.match(rel_posix) for rx in self._compiled)


def apply_excludes(site_packages: Path, excluder: Excluder) -> list[str]:
    """Delete excluded files below ``site_packages`` and drop them from RECORD files."""
    if not excluder:
        return []
    removed: list[str] = []
    for path in sorted(site_packages.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(site_packages).as_posix()
        if rel.endswith(".dist-info/RECORD") or ".dist-info/" in rel and rel.count("/") == 1:
            continue  # metadata must stay intact
        if excluder.matches(rel):
            path.unlink()
            removed.append(rel)
    if removed:
        gone = set(removed)
        for record in site_packages.glob("*.dist-info/RECORD"):
            _rewrite_record(record, site_packages, gone)
        _prune_empty_dirs(site_packages)
    return removed


def _rewrite_record(record: Path, site_packages: Path, gone: set[str]) -> None:
    rows = list(csv.reader(io.StringIO(record.read_text(encoding="utf-8"))))
    kept = []
    for row in rows:
        if not row:
            continue
        rel = posixpath.normpath(row[0])
        if rel in gone:
            continue
        kept.append(row)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=",", quotechar='"', lineterminator="\n")
    writer.writerows(kept)
    record.write_text(buf.getvalue(), encoding="utf-8", newline="")


def _prune_empty_dirs(root: Path) -> None:
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        if not any(d.iterdir()):
            d.rmdir()


def iter_files(root: Path) -> list[PurePosixPath]:
    return sorted(PurePosixPath(p.relative_to(root).as_posix())
                  for p in root.rglob("*") if p.is_file())
