"""The build pipeline: config → runtime → lock → wheels → install → launchers → checks."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from importlib.resources import files as resources_files
from pathlib import Path

from . import __version__, doctor
from .config import ProjectConfig
from .download import fetch, offline_mode
from .errors import PyxbuildError, ResolveError
from .imports import check_imports
from .install import Excluder, InstallState, apply_excludes, install_wheel
from .launcher import build_launcher
from .local import resolve_local
from .lock import (
    LockInputs,
    compile_lock,
    read_lock,
    requirement_lines,
    select_files,
)
from .project import BuiltWheel, build_wheel
from .resources import launcher_resources
from .runtime import Runtime, get_runtime
from .targets import Target, get_target

RUNTIME_DIR = "runtime"

# Parts of the standard library an application can drop ([tool.pyxbuild]
# slim = [...]). Patterns are relative to the runtime folder.
SLIM_PRESETS: dict[str, list[str]] = {
    "tests": ["DLLs/_test*.pyd", "DLLs/_ctypes_test.pyd", "DLLs/xxlimited*.pyd",
              "DLLs/xxsubtype.pyd", "Lib/test/", "Lib/idlelib/idle_test/",
              "Lib/tkinter/test/"],
    "idle": ["Lib/idlelib/"],
    "ensurepip": ["Lib/ensurepip/"],
    "turtledemo": ["Lib/turtledemo/"],
    "lib2to3": ["Lib/lib2to3/"],
    "pydoc": ["Lib/pydoc_data/"],
    "venv": ["Lib/venv/"],
    "tk": ["Lib/tkinter/", "Lib/turtle.py", "Lib/idlelib/", "Lib/turtledemo/", "tcl/",
           "DLLs/_tkinter.pyd", "DLLs/tcl*.dll", "DLLs/tk*.dll"],
    "sqlite": ["Lib/sqlite3/", "DLLs/_sqlite3.pyd", "DLLs/sqlite3.dll"],
}
DEFAULT_SLIM = ("tests", "idle", "ensurepip", "turtledemo", "lib2to3")
# The same presets for POSIX runtimes: {stdlib} is lib/python3.X, extension
# modules live in {stdlib}/lib-dynload, shared libraries in lib/.
SLIM_PRESETS_POSIX: dict[str, list[str]] = {
    "tests": ["{stdlib}/lib-dynload/_test*.so", "{stdlib}/lib-dynload/_ctypes_test*.so",
              "{stdlib}/lib-dynload/xxlimited*.so", "{stdlib}/lib-dynload/xxsubtype*.so",
              "{stdlib}/test/", "{stdlib}/idlelib/idle_test/", "{stdlib}/tkinter/test/"],
    "idle": ["{stdlib}/idlelib/"],
    "ensurepip": ["{stdlib}/ensurepip/"],
    "turtledemo": ["{stdlib}/turtledemo/"],
    "lib2to3": ["{stdlib}/lib2to3/"],
    "pydoc": ["{stdlib}/pydoc_data/"],
    "venv": ["{stdlib}/venv/"],
    "tk": ["{stdlib}/tkinter/", "{stdlib}/turtle.py", "{stdlib}/idlelib/",
           "{stdlib}/turtledemo/", "{stdlib}/lib-dynload/_tkinter*.so", "lib/libtcl*",
           "lib/libtk*", "lib/tcl*/", "lib/tk*/", "lib/itcl*/", "lib/thread*/"],
    "sqlite": ["{stdlib}/sqlite3/", "{stdlib}/lib-dynload/_sqlite3*.so"],
}


def slim_patterns(target: Target, python: str, name: str) -> list[str]:
    if target.is_windows:
        return SLIM_PRESETS[name]
    stdlib = target.stdlib_dir(python)
    return [p.replace("{stdlib}", stdlib) for p in SLIM_PRESETS_POSIX[name]]
# Runtime parts that only matter for compiling extensions.
RUNTIME_SKIP = ("include", "libs")
DEFAULT_EPOCH = 315532800  # 1980-01-01, the earliest time a zip file can store


@dataclass
class BuildOptions:
    targets: list[str] | None = None
    python: str | None = None
    mode: str | None = None
    zip: bool | None = None
    frozen: bool = False
    """Fail instead of re-resolving when the lock file is missing or stale."""
    relock: bool = False
    """Re-resolve even if the lock file is up to date."""
    out_dir: Path | None = None
    quiet: bool = False
    offline: bool = False
    """Use only the download cache (implies ``frozen``)."""
    strict: bool = False
    """Fail the build when the bundle check reports errors."""


@dataclass
class TargetBuild:
    target: Target
    python: str
    runtime: Runtime
    env: dict[str, str]
    project_wheel: BuiltWheel
    lock_path: Path
    wheels: list[Path] = field(default_factory=list)
    local_wheels: list[Path] = field(default_factory=list)  # workspace / path sources


@dataclass
class BuildResult:
    target: str
    python_version: str
    app_dir: Path
    launchers: list[Path]
    zip_path: Path | None
    report: doctor.Report


def _log(opts: BuildOptions, msg: str) -> None:
    if not opts.quiet:
        print(msg, file=sys.stderr)


def source_date_epoch() -> int:
    try:
        return int(os.environ.get("SOURCE_DATE_EPOCH", DEFAULT_EPOCH))
    except ValueError:
        return DEFAULT_EPOCH


def prepare(cfg: ProjectConfig, target: Target, opts: BuildOptions, work: Path,
            *, download: bool = True, lock_base: Path | None = None) -> TargetBuild:
    """Everything up to (and including) downloading wheels; writes only the lock file."""
    python = opts.python or cfg.choose_python()
    _log(opts, f"[{target.name}] Python {python} runtime ({cfg.runtime})")
    runtime = get_runtime(target, python, quiet=opts.quiet)
    if cfg.requires_python is not None and not cfg.requires_python.contains(
            runtime.spec.version, prereleases=True):
        raise PyxbuildError(
            f"requires-python {cfg.requires_python} excludes the bundled Python "
            f"{runtime.spec.version}",
            hint="choose another 'python', or update the pinned runtimes")
    env = target.marker_environment(runtime.spec.version, runtime.marker_overrides())

    _log(opts, f"[{target.name}] building wheel for {cfg.name}")
    wheel = build_wheel(cfg.root, work / "project", quiet=True)
    if not wheel.is_pure:
        raise PyxbuildError(
            f"the project wheel {wheel.path.name} is platform-specific",
            hint="pyxbuild cannot cross-compile extension modules; build that part as a "
                 f"separate package with {target.name} wheels (e.g. with cibuildwheel)",
        )

    local = resolve_local(
        cfg.root, wheel.name, wheel.requires_dist, cfg.extras,
        lambda reqs, extras, owner: requirement_lines(reqs, target, env, extras,
                                                      project=owner),
        work / "local")
    if local.wheels:
        _log(opts, f"[{target.name}] built local package(s): "
             + ", ".join(f"{w.name} {w.version}" for w in local.wheels))
    requirements = local.requirements
    inputs = LockInputs(
        requirements=tuple(requirements), target=target.name,
        python_version=runtime.spec.version, index_url=cfg.index_url,
        extra_index_urls=tuple(cfg.extra_index_urls), allow_sdist=cfg.sdists_allowed,
        find_links=tuple(cfg.find_links), find_links_key=tuple(cfg.find_links_cfg),
        no_binary=tuple(cfg.only_sdist),
    )
    lock_path = cfg.lock_path(target.name)
    lock = read_lock(lock_path) if lock_path.exists() else None
    if lock is not None and lock.inputs_digest is None:
        # a PEP 751 lock written by another tool: use it, never overwrite it
        if opts.relock:
            raise ResolveError(
                f"{lock_path} was not written by pyxbuild; refusing to overwrite it",
                hint="remove it, or point 'lock' at another file")
        _log(opts, f"[{target.name}] using {lock_path.name} as is (not written by pyxbuild)")
        fresh = True
    else:
        fresh = lock is not None and lock.inputs_digest == inputs.digest()
    if not fresh or opts.relock:
        if opts.frozen:
            state = "missing" if lock is None else "out of date"
            raise ResolveError(f"lock file {lock_path} is {state}",
                               hint="run `pyxbuild lock` and commit the result")
        from .lock import resolver_name

        engine = "uv" if resolver_name() == "uv" else "the built-in resolver"
        _log(opts, f"[{target.name}] resolving {len(requirements)} requirement(s) with {engine}")
        compile_lock(inputs, target, runtime.spec.version, lock_path, quiet=True)
        lock = read_lock(lock_path)
    assert lock is not None
    selections = select_files(lock, target, env, target.tags(python),
                              allow_sdist=cfg.sdists_allowed, project=wheel.name)

    tb = TargetBuild(target, python, runtime, env, wheel, lock_path,
                     local_wheels=[w.path for w in local.wheels])
    local_names = set(local.names)
    for sel in selections if download else []:
        if sel.file is None:  # directory entry of a lock written by another tool
            if sel.package.name in local_names:
                continue  # already built from [tool.uv.sources]
            folder = (lock_base or lock_path.parent) / sel.package.directory
            _log(opts, f"[{target.name}] building local package {sel.package.name} "
                 f"from {sel.package.directory}")
            built = build_wheel(folder, work / "local-lock", quiet=True)
            if not built.is_pure:
                raise PyxbuildError(
                    f"the local package {built.path.name} is platform-specific",
                    hint=f"build it for {target.name} elsewhere and add it through find-links")
            tb.wheels.append(built.path)
            continue
        path = fetch(_absolute_url(sel.file.url, lock_base or lock_path.parent),
                     sel.file.sha256, sel.file.name, quiet=opts.quiet)
        if not sel.is_wheel and sel.package.name in cfg.pure_python:
            from .fallbacks import pure_python_env

            built = build_wheel(path, work / "pure", quiet=True,
                                env=pure_python_env(sel.package.name, dict(os.environ)))
            if not built.is_pure:
                raise PyxbuildError(
                    f"{sel.package.name}: building without its C extension still produced "
                    f"{built.path.name}", hint="the project's build switch may have changed")
            path = built.path
        elif not sel.is_wheel and cfg.may_cross_compile(sel.package.name) \
                and target.is_windows:
            from .crossbuild import cross_build_wheel

            _log(opts, f"[{target.name}] cross-compiling {sel.file.name} with zig (experimental)")
            path = cross_build_wheel(path, runtime, work / "cross", quiet=True).path
        elif not sel.is_wheel:
            built = build_wheel(path, work / "sdists", quiet=True)
            if not built.is_pure:
                raise PyxbuildError(
                    f"{sel.package.name}: the sdist builds a platform-specific wheel on this "
                    f"host, which cannot run on {target.name}",
                    hint=f"pin a version with a {target.name} wheel, or try "
                         f"cross-compile = [\"{sel.package.name}\"] (experimental)",
                )
            path = built.path
        tb.wheels.append(path)
    return tb


def _absolute_url(url: str, base: Path) -> str:
    """PEP 751 ``path`` entries are relative to the lock file's directory."""
    if "://" in url:
        return url
    return str((base / url).resolve())


def _copy_runtime(src: Path, dest: Path, slim: list[str] | None = None, *,
                  keep_dev_files: bool = False, target: Target | None = None,
                  python: str = "") -> int:
    """Copy the runtime, leaving out what the bundle must not contain.

    Skipped: ``include/``, ``libs/``, ``__pycache__``, everything in
    ``site-packages`` (python-build-standalone preinstalls pip, and setuptools
    for 3.11, which could clash with locked packages: torch on 3.11), and the
    ``slim`` presets. Returns the number of bytes left out by ``slim``.
    """
    target = target or get_target("win_amd64")
    excluder = Excluder([p for name in slim or [] for p in slim_patterns(target, python, name)])
    site = src / target.site_packages(python) if not target.is_windows else (
        src / "Lib" / "site-packages")
    skipped = 0

    def ignore(directory: str, names: list[str]) -> set[str]:
        nonlocal skipped
        d = Path(directory)
        if not target.is_windows and not keep_dev_files and d in posix_skip:
            out = {n for n in names if posix_skip[d](n)}
        elif d == src:
            out = set() if keep_dev_files else {n for n in names if n.lower() in RUNTIME_SKIP}
        elif d == site:
            out = {n for n in names if n != "README.txt"}
        else:
            out = {n for n in names if n == "__pycache__"}
        if excluder:
            rel = d.relative_to(src).as_posix()
            for n in names:
                if n in out:
                    continue
                path = f"{rel}/{n}" if rel != "." else n
                full = d / n
                # match directories by their contents' prefix ("tcl/" -> "tcl/x")
                probe = path + "/x" if full.is_dir() else path
                if excluder.matches(probe):
                    out.add(n)
                    skipped += (sum(f.stat().st_size for f in full.rglob("*") if f.is_file())
                                if full.is_dir() else full.stat().st_size)
        return out

    # POSIX runtimes: only the interpreter binary from bin/ (the rest are
    # symlinks and pip/idle scripts), no libpython*.so (the interpreter is
    # static; the .so only serves embedding), headers or pkg-config files, and
    # no share/ (man pages; a terminfo copy whose names differ only by case,
    # which case-insensitive build hosts would merge; curses falls back to the
    # system's terminfo)
    posix_skip = {
        src: lambda n: n in ("include", "share"),
        src / "bin": lambda n: n != f"python{python}",
        src / "lib": lambda n: n == "pkgconfig" or n.startswith("libpython"),
    }
    shutil.copytree(src, dest, ignore=ignore)
    return skipped


def compile_bytecode(runtime_dir: Path, python: str, stdlib: str = "Lib") -> None:
    """Write __pycache__/*.pyc for the whole runtime with a host CPython ``python``.

    ``.pyc`` files depend only on the X.Y version, not on the OS. The
    ``unchecked-hash`` mode never compares them with the source, so the app
    neither recompiles on first start nor needs a writable folder, and the
    embedded paths are made relative (``-s``) so the result is reproducible.
    """
    from .lock import uv_executable

    # Our own driver instead of compileall: the path embedded in each .pyc is
    # always "Lib/x.py" with "/" (compileall -s/-p would use "\\" on Windows
    # hosts) and files are processed in sorted order.
    driver = (
        "import os,sys,py_compile\n"
        "root=sys.argv[1]\n"
        "mode=py_compile.PycInvalidationMode.UNCHECKED_HASH\n"
        "bad=0\n"
        "for d,ds,fs in os.walk(os.path.join(root,sys.argv[2])):\n"
        "    ds.sort()\n"
        "    for f in sorted(fs):\n"
        "        if not f.endswith('.py'): continue\n"
        "        src=os.path.join(d,f)\n"
        "        rel=os.path.relpath(src,root).replace(os.sep,'/')\n"
        "        try: py_compile.compile(src,dfile=rel,doraise=True,invalidation_mode=mode)\n"
        "        except py_compile.PyCompileError: bad+=1\n"
        "print(bad)\n"
    )
    if sys.platform == "emscripten":  # no processes: compile here if the versions match
        if f"{sys.version_info[0]}.{sys.version_info[1]}" != python:
            raise PyxbuildError(
                f"bytecode = true needs Python {python} to compile, and this browser runs "
                f"Python {sys.version_info[0]}.{sys.version_info[1]}",
                hint=f'set python = "{sys.version_info[0]}.{sys.version_info[1]}" or '
                     "bytecode = false")
        import contextlib
        import io

        old_argv = sys.argv
        sys.argv = ["-c", str(runtime_dir), stdlib]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                exec(compile(driver, "<pyxbuild-bytecode>", "exec"), {"__name__": "__main__"})
        finally:
            sys.argv = old_argv
        return
    cmd = [uv_executable(), "run", "--no-project", "--no-config", "--python", python, "--",
           "python", "-I", "-c", driver, str(runtime_dir), stdlib]
    env = {**os.environ, "UV_NO_PROGRESS": "1", "PYTHONHASHSEED": "0"}
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    # files with syntax errors (test data) simply stay without bytecode
    if proc.returncode != 0:
        raise PyxbuildError(f"compiling bytecode failed:\n{proc.stderr.strip()}")


def _normalize_tree(root: Path, epoch: int, *, windows: bool = True) -> None:
    for path in sorted(root.rglob("*"), key=lambda p: -len(p.parts)):
        if path.is_file():
            if windows:
                executable = path.suffix.lower() in (".exe", ".dll", ".pyd")
            else:  # by content, so that every build host gives the same modes
                executable = posix_executable(path)
            path.chmod(0o755 if executable else 0o644)
        os.utime(path, (epoch, epoch))
    os.utime(root, (epoch, epoch))


def posix_executable(path: Path) -> bool:
    """ELF images and scripts: the onefile launcher uses the same rule."""
    with open(path, "rb") as f:
        head = f.read(4)
    return head == b"\x7fELF" or head[:2] == b"#!"


def write_tarball(app_dir: Path, tar_path: Path, epoch: int) -> None:
    """Deterministic .tar.gz (POSIX targets keep executable bits): sorted
    entries, fixed owner and timestamps, no gzip name or time."""
    import gzip
    import tarfile

    tmp = tar_path.with_name(tar_path.name + ".partial")
    with open(tmp, "wb") as raw, \
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for path in [app_dir, *sorted(app_dir.rglob("*"),
                                      key=lambda p: p.relative_to(app_dir).as_posix())]:
            rel = app_dir.name if path == app_dir else \
                f"{app_dir.name}/{path.relative_to(app_dir).as_posix()}"
            info = tarfile.TarInfo(rel)
            info.mtime = max(epoch, 0)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
            else:
                info.mode = 0o755 if posix_executable(path) else 0o644
                info.size = path.stat().st_size
                with open(path, "rb") as f:
                    tf.addfile(info, f)
    os.replace(tmp, tar_path)


def write_zip(app_dir: Path, zip_path: Path, epoch: int) -> None:
    """Deterministic zip: sorted entries, fixed timestamps and permissions."""
    import time

    date_time = time.gmtime(max(epoch, DEFAULT_EPOCH))[:6]
    tmp = zip_path.with_suffix(".zip.partial")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(app_dir.rglob("*"), key=lambda p: p.relative_to(app_dir).as_posix()):
            if not path.is_file():
                continue
            arcname = f"{app_dir.name}/{path.relative_to(app_dir).as_posix()}"
            info = zipfile.ZipInfo(arcname, date_time=date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 0
            with open(path, "rb") as f:
                zf.writestr(info, f.read(), compresslevel=9)
    os.replace(tmp, zip_path)


def assemble(cfg: ProjectConfig, tb: TargetBuild, app_dir: Path, opts: BuildOptions,
             *, launchers: bool = True) -> tuple[list[Path], doctor.Report]:
    """Lay out the application folder in ``app_dir`` (which must not exist)."""
    runtime_dir = app_dir / RUNTIME_DIR
    target = tb.target
    saved = _copy_runtime(tb.runtime.path, runtime_dir, cfg.slim,
                          keep_dev_files=cfg.keep_dev_files, target=target, python=tb.python)
    if saved:
        _log(opts, f"[{tb.target.name}] slim: removed {saved / 2**20:.1f} MB of the "
             f"standard library ({', '.join(cfg.slim)})")
    _log(opts, f"[{tb.target.name}] installing "
         f"{len(tb.wheels) + len(tb.local_wheels) + 1} wheel(s)")
    state = InstallState(runtime_dir, Excluder(cfg.exclude), target, tb.python)
    for wheel in sorted(tb.wheels, key=lambda p: p.name.lower()):
        install_wheel(wheel, runtime_dir, state)
    for wheel in tb.local_wheels:
        install_wheel(wheel, runtime_dir, state)
    install_wheel(tb.project_wheel.path, runtime_dir, state)
    site_packages = runtime_dir / target.site_packages(tb.python)
    # excluded files are skipped while installing; this catches anything left
    removed = state.excluded + apply_excludes(site_packages, Excluder(cfg.exclude))
    if removed:
        _log(opts, f"[{tb.target.name}] excluded {len(removed)} file(s)")
    boot = resources_files("pyxbuild").joinpath("_boot")
    for module in ("_pyxbuild_boot.py", "_pyxbuild_streams.py"):
        (runtime_dir / target.stdlib_dir(tb.python) / module).write_bytes(
            boot.joinpath(module).read_bytes())
    # imported by site at the start of every interpreter of the bundle,
    # child processes included (see _pyxbuild_streams.py)
    (site_packages / "_pyxbuild_streams.pth").write_bytes(b"import _pyxbuild_streams\n")
    if cfg.bytecode:
        _log(opts, f"[{tb.target.name}] compiling bytecode with host Python {tb.python}")
        compile_bytecode(runtime_dir, tb.python, target.stdlib_dir(tb.python))

    created: list[Path] = []
    if launchers:
        for entry in cfg.entry_points:
            exe = app_dir / target.launcher_name(entry.name)
            if target.is_windows:
                res = launcher_resources(
                    script=entry.name, project_name=tb.project_wheel.name,
                    version=tb.project_wheel.version, description=tb.project_wheel.summary,
                    version_info_cfg=cfg.version_info, icon=cfg.icon,
                )
                exe.write_bytes(build_launcher(entry, target.name, python_rel=RUNTIME_DIR,
                                               utf8_mode=cfg.utf8_mode, resources=res,
                                               boot=True))
            else:
                exe.write_bytes(build_launcher(
                    entry, target.name, python_rel=RUNTIME_DIR, utf8_mode=cfg.utf8_mode,
                    boot=True,
                    interpreter=f"{RUNTIME_DIR}/{target.interpreter(tb.python, entry.gui)}"))
                exe.chmod(0o755)
            created.append(exe)

    info = {
        "pyxbuild": __version__,
        "project": {"name": tb.project_wheel.name, "version": tb.project_wheel.version},
        "target": tb.target.name,
        "python": tb.runtime.spec.version,
        "runtime": {"source": cfg.runtime, "release": tb.runtime.spec.release,
                    "file": tb.runtime.spec.file, "sha256": tb.runtime.spec.sha256},
        "launchers": {e.name: {"entry-point": f"{e.module}:{e.attr}", "gui": e.gui}
                      for e in cfg.entry_points},
        "boot": cfg.boot_settings(),
    }
    # newline="\n": text mode would write CRLF on Windows and break
    # byte-identical builds across hosts
    (runtime_dir / "pyxbuild.json").write_text(json.dumps(info, indent=2) + "\n",
                                                encoding="utf-8", newline="\n")
    _normalize_tree(app_dir, source_date_epoch(), windows=target.is_windows)
    if not target.is_windows:  # the DLL and import checks know Windows only
        report = doctor.Report()
        state.add_findings(report)
        return created, report
    report = doctor.check_bundle(app_dir, runtime_dir)
    state.add_findings(report)
    check_imports(runtime_dir, tb.project_wheel.path, report)
    return created, report


def build(cfg: ProjectConfig, opts: BuildOptions | None = None) -> list[BuildResult]:
    opts = opts or BuildOptions()
    if opts.offline:
        opts.frozen = True
        with offline_mode():
            return _build(cfg, opts)
    return _build(cfg, opts)


def _build(cfg: ProjectConfig, opts: BuildOptions) -> list[BuildResult]:
    mode = opts.mode or cfg.mode
    if mode not in ("folder", "onefile"):
        raise PyxbuildError(f"unknown mode {mode!r}", hint='use "folder" or "onefile"')
    dist = (cfg.root / (opts.out_dir or cfg.dist_dir)).resolve()
    dist.mkdir(parents=True, exist_ok=True)
    make_zip = cfg.zip if opts.zip is None else opts.zip
    results = []
    for target_name in opts.targets or cfg.targets:
        target = get_target(target_name)
        with tempfile.TemporaryDirectory(prefix=".pyxbuild-", dir=dist) as tmp:
            work = Path(tmp)
            tb = prepare(cfg, target, opts, work)
            staging = work / "app"
            launchers, report = assemble(cfg, tb, staging, opts,
                                         launchers=mode == "folder")
            shown = [f for f in report.findings if f.level != doctor.INFO]
            if shown:
                _log(opts, f"[{target.name}] bundle check:\n"
                     f"{doctor.Report(shown).format()}")
            if len(shown) < len(report.findings):
                _log(opts, f"[{target.name}] {len(report.findings) - len(shown)} informational "
                     f"note(s); run `pyxbuild doctor` to see them")
            if report.errors and opts.strict:
                raise PyxbuildError(
                    f"bundle check found {len(report.errors)} error(s)",
                    hint="fix them, or build without --strict",
                )
            if mode == "onefile":
                exes = _write_onefile(cfg, tb, staging, work, dist, opts)
                if make_zip:
                    _log(opts, f"[{target.name}] zip skipped: a onefile build is one file")
                _log(opts, f"[{target.name}] built " + ", ".join(str(e) for e in exes))
                results.append(BuildResult(target.name, tb.runtime.spec.version, dist, exes,
                                           None, report))
                continue
            app_dir = dist / f"{cfg.normalized_name}-{target.name}"
            if app_dir.exists():
                shutil.rmtree(app_dir)
            os.replace(staging, app_dir)
        launchers = [app_dir / p.name for p in launchers]
        zip_path = None
        if make_zip and target.is_windows:
            zip_path = dist / f"{cfg.normalized_name}-{tb.project_wheel.version}-{target.name}.zip"
            write_zip(app_dir, zip_path, source_date_epoch())
        elif make_zip:  # .tar.gz keeps executable bits
            zip_path = (dist / f"{cfg.normalized_name}-{tb.project_wheel.version}-"
                        f"{target.name}.tar.gz")
            write_tarball(app_dir, zip_path, source_date_epoch())
        _log(opts, f"[{target.name}] built {app_dir}")
        results.append(BuildResult(target.name, tb.runtime.spec.version, app_dir, launchers,
                                   zip_path, report))
    return results


def _write_onefile(cfg: ProjectConfig, tb: TargetBuild, staging: Path, work: Path,
                   dist: Path, opts: BuildOptions) -> list[Path]:
    """Pack the assembled folder into one executable per entry point."""
    from .launcher import write_onefile
    from .onefile import write_archive

    _log(opts, f"[{tb.target.name}] packing the application into one file "
         f"(extract: {cfg.onefile_extract})")
    archive = write_archive(staging, work / "app.pyxarc")
    if len(cfg.entry_points) > 1:
        _log(opts, f"[{tb.target.name}] note: each of the {len(cfg.entry_points)} "
             "executables carries the whole application; they share one extracted copy")
    exes = []
    for entry in cfg.entry_points:
        target = tb.target
        res = launcher_resources(
            script=entry.name, project_name=tb.project_wheel.name,
            version=tb.project_wheel.version, description=tb.project_wheel.summary,
            version_info_cfg=cfg.version_info, icon=cfg.icon,
        ) if target.is_windows else None
        interpreter = None if target.is_windows else \
            f"{RUNTIME_DIR}/{target.interpreter(tb.python, entry.gui)}"
        exe = dist / target.launcher_name(entry.name)
        partial = exe.with_name(exe.name + ".partial")
        write_onefile(partial, entry, target.name, archive, app=cfg.normalized_name,
                      extract=cfg.onefile_extract, utf8_mode=cfg.utf8_mode,
                      progress=cfg.onefile_progress,
                      title=cfg.version_info.get("product-name") or cfg.name, resources=res,
                      interpreter=interpreter)
        if not target.is_windows:
            partial.chmod(0o755)
        os.replace(partial, exe)
        exes.append(exe)
    _log(opts, f"[{tb.target.name}] {archive.files} files, "
         f"{archive.stream_size / 2**20:.1f} MB -> {archive.size / 2**20:.1f} MB")
    return exes


def lock_only(cfg: ProjectConfig, opts: BuildOptions) -> list[Path]:
    opts.relock = True
    paths = []
    for target_name in opts.targets or cfg.targets:
        target = get_target(target_name)
        with tempfile.TemporaryDirectory(prefix="pyxbuild-") as tmp:
            tb = prepare(cfg, target, opts, Path(tmp), download=False)
        paths.append(tb.lock_path)
    return paths


def _report_held_back(tb: TargetBuild, report: doctor.Report) -> None:
    from .fallbacks import PURE_PYTHON_SWITCHES, held_back

    locked = [(p.name, p.version) for p in read_lock(tb.lock_path).packages if p.version]
    for name, version, latest, has_wheel, has_sdist in held_back(
            locked, tb.target.tags(tb.python)):
        if has_wheel:
            continue  # newer release exists but the lock is just older: run `pyxbuild lock`
        if not has_sdist:
            options = [f"wait for {latest} wheels for {tb.target.name}",
                       "use an older Python with --python"]
        else:
            options = ["build its wheel on Windows CI and add it via find-links",
                       f'cross-compile = ["{name}"] (experimental, self-contained C only)']
            if name in PURE_PYTHON_SWITCHES:
                options.insert(0, f'pure-python = ["{name}"]')
        report.add(doctor.WARNING,
                   f"{name} is held back at {version}: {latest} has no wheel for "
                   f"{tb.target.name} / Python {tb.python}; to use {latest}: "
                   + "; or ".join(options))


def check(cfg: ProjectConfig, opts: BuildOptions) -> list[tuple[str, doctor.Report]]:
    """``pyxbuild doctor``: resolve, install into a scratch folder, report problems."""
    out = []
    for target_name in opts.targets or cfg.targets:
        target = get_target(target_name)
        report = doctor.Report()
        with tempfile.TemporaryDirectory(prefix="pyxbuild-doctor-") as tmp:
            # never touch the project's lock file: work on a copy
            scratch_lock = Path(tmp) / "pylock.toml"
            if cfg.lock_path(target_name).exists():
                shutil.copyfile(cfg.lock_path(target_name), scratch_lock)
            scratch_cfg = dataclasses.replace(cfg, lock=scratch_lock)
            try:
                tb = prepare(scratch_cfg, target, opts, Path(tmp),
                             lock_base=cfg.lock_path(target_name).parent)
            except PyxbuildError as exc:
                report.add(doctor.ERROR, str(exc))
            else:
                _, report = assemble(scratch_cfg, tb, Path(tmp) / "app", opts,
                                     launchers=False)
                if not cfg.index_url:
                    _report_held_back(tb, report)
        out.append((target_name, report))
    return out
