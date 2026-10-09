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
from pathlib import Path, PurePosixPath

from installer import install
from installer.destinations import SchemeDictionaryDestination
from installer.records import RecordEntry
from installer.sources import WheelFile
from installer.utils import Scheme, construct_record_file

from .errors import PyxbuildError

INSTALLER_NAME = b"pyxbuild"


def nt_scheme(runtime_dir: Path, dist_name: str) -> dict[str, str]:
    """``sysconfig`` ``nt`` scheme for an interpreter living in ``runtime_dir``."""
    return {
        "purelib": str(runtime_dir / "Lib" / "site-packages"),
        "platlib": str(runtime_dir / "Lib" / "site-packages"),
        "headers": str(runtime_dir / "Include" / dist_name),
        "scripts": str(runtime_dir / "Scripts"),
        "data": str(runtime_dir),
    }


class _Destination(SchemeDictionaryDestination):
    """Like the base class, but RECORD paths never depend on the host OS.

    ``installer`` writes absolute paths for out-of-scheme files when it runs
    on Windows; pip writes ``../../Scripts/x.exe``. We always write the
    relative POSIX form so the bundle is byte-identical on every host.
    """

    target = "win_amd64"

    def write_to_fs(self, scheme: Scheme, path: str, stream, is_executable: bool):
        # Defence in depth (installer >= 1.0 checks this too): a wheel entry
        # must stay inside its scheme directory.
        parts = PurePosixPath(path.replace("\\", "/")).parts
        if (not parts or ".." in parts or path.startswith(("/", "\\"))
                or ":" in path):
            raise PyxbuildError(f"refusing wheel entry outside the install folder: {path!r}")
        root = Path(self.scheme_dict[scheme]).resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise PyxbuildError(f"refusing wheel entry outside the install folder: {path!r}")
        return super().write_to_fs(scheme, path, stream, is_executable)

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

        record_list = list(records)
        with construct_record_file(record_list, prefix_for_scheme) as stream:
            self.write_to_fs(scheme, record_file_path, stream, is_executable=False)


def install_wheel(wheel_path: Path, runtime_dir: Path) -> None:
    from packaging.utils import parse_wheel_filename

    name = parse_wheel_filename(wheel_path.name)[0]
    destination = _Destination(
        nt_scheme(runtime_dir, str(name)),
        interpreter="python.exe",
        script_kind="win-amd64",
        bytecode_optimization_levels=(),
    )
    try:
        with warnings.catch_warnings():
            # installer skips bytecode shipped inside wheels (PySide6 does this)
            # and warns per file; Python regenerates it, so stay quiet.
            warnings.filterwarnings("ignore", message="Skip installing .*__pycache__")
            with WheelFile.open(wheel_path) as source:
                install(source, destination, {"INSTALLER": INSTALLER_NAME})
    except FileExistsError as exc:
        raise PyxbuildError(
            f"{wheel_path.name} overwrites a file installed by another package: {exc}"
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
