"""The build pipeline: config → runtime → lock → wheels → install → launchers → checks."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, doctor
from .config import ProjectConfig
from .download import fetch, offline_mode
from .errors import PyxbuildError, ResolveError
from .install import Excluder, apply_excludes, install_wheel
from .launcher import build_launcher
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
            *, download: bool = True) -> TargetBuild:
    """Everything up to (and including) downloading wheels; writes only the lock file."""
    python = opts.python or cfg.choose_python()
    _log(opts, f"[{target.name}] Python {python} runtime ({cfg.runtime})")
    runtime = get_runtime(target, python, quiet=opts.quiet)
    env = target.marker_environment(runtime.spec.version, runtime.marker_overrides())

    _log(opts, f"[{target.name}] building wheel for {cfg.name}")
    wheel = build_wheel(cfg.root, work / "project", quiet=True)
    if not wheel.is_pure:
        raise PyxbuildError(
            f"the project wheel {wheel.path.name} is platform-specific",
            hint="pyxbuild cannot cross-compile extension modules; build that part as a "
                 "separate package with win_amd64 wheels (e.g. with cibuildwheel)",
        )

    requirements = requirement_lines(wheel.requires_dist, target, env, cfg.extras)
    inputs = LockInputs(
        requirements=tuple(requirements), target=target.name,
        python_version=runtime.spec.version, index_url=cfg.index_url,
        extra_index_urls=tuple(cfg.extra_index_urls), allow_sdist=cfg.allow_sdist,
        find_links=tuple(cfg.find_links),
    )
    lock_path = cfg.lock_path(target.name)
    lock = read_lock(lock_path) if lock_path.exists() else None
    fresh = lock is not None and lock.inputs_digest == inputs.digest()
    if not fresh or opts.relock:
        if opts.frozen:
            state = "missing" if lock is None else "out of date"
            raise ResolveError(f"lock file {lock_path} is {state}",
                               hint="run `pyxbuild lock` and commit the result")
        _log(opts, f"[{target.name}] resolving {len(requirements)} requirement(s) with uv")
        compile_lock(inputs, target, runtime.spec.version, lock_path, quiet=True)
        lock = read_lock(lock_path)
    assert lock is not None
    selections = select_files(lock, target, env, target.tags(python),
                              allow_sdist=cfg.allow_sdist)

    tb = TargetBuild(target, python, runtime, env, wheel, lock_path)
    for sel in selections if download else []:
        path = fetch(_absolute_url(sel.file.url, lock_path), sel.file.sha256, sel.file.name,
                     quiet=opts.quiet)
        if not sel.is_wheel:
            built = build_wheel(path, work / "sdists", quiet=True)
            if not built.is_pure:
                raise PyxbuildError(
                    f"{sel.package.name}: the sdist builds a platform-specific wheel on this "
                    f"host, which cannot run on {target.name}",
                    hint="pin a version with a win_amd64 wheel",
                )
            path = built.path
        tb.wheels.append(path)
    return tb


def _absolute_url(url: str, lock_path: Path) -> str:
    if "://" in url:
        return url
    return str((lock_path.parent / url).resolve())


def _copy_runtime(src: Path, dest: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        if Path(directory) == src:
            return {n for n in names if n.lower() in RUNTIME_SKIP}
        return {n for n in names if n == "__pycache__"}

    shutil.copytree(src, dest, ignore=ignore)
    # python-build-standalone preinstalls pip (and setuptools for 3.11). The
    # bundle must contain exactly the locked packages: anything else could
    # clash with them (torch requires its own setuptools on 3.11).
    site = dest / "Lib" / "site-packages"
    for item in sorted(site.iterdir()):
        if item.name == "README.txt":
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()


def _normalize_tree(root: Path, epoch: int) -> None:
    for path in sorted(root.rglob("*"), key=lambda p: -len(p.parts)):
        if path.is_file():
            path.chmod(0o755 if path.suffix.lower() in (".exe", ".dll", ".pyd") else 0o644)
        os.utime(path, (epoch, epoch))
    os.utime(root, (epoch, epoch))


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
    _copy_runtime(tb.runtime.path, runtime_dir)
    _log(opts, f"[{tb.target.name}] installing {len(tb.wheels) + 1} wheel(s)")
    for wheel in sorted(tb.wheels, key=lambda p: p.name.lower()):
        install_wheel(wheel, runtime_dir)
    install_wheel(tb.project_wheel.path, runtime_dir)
    site_packages = runtime_dir / "Lib" / "site-packages"
    removed = apply_excludes(site_packages, Excluder(cfg.exclude))
    if removed:
        _log(opts, f"[{tb.target.name}] excluded {len(removed)} file(s)")

    created: list[Path] = []
    if launchers:
        for entry in cfg.entry_points:
            exe = app_dir / f"{entry.name}{tb.target.exe_suffix}"
            res = launcher_resources(
                script=entry.name, project_name=tb.project_wheel.name,
                version=tb.project_wheel.version, description=tb.project_wheel.summary,
                version_info_cfg=cfg.version_info, icon=cfg.icon,
            )
            exe.write_bytes(build_launcher(entry, tb.target.name, python_rel=RUNTIME_DIR,
                                           utf8_mode=cfg.utf8_mode, resources=res))
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
    }
    # newline="\n": text mode would write CRLF on Windows and break
    # byte-identical builds across hosts
    (runtime_dir / "pyxbuild.json").write_text(json.dumps(info, indent=2) + "\n",
                                                encoding="utf-8", newline="\n")
    _normalize_tree(app_dir, source_date_epoch())
    report = doctor.check_bundle(app_dir, runtime_dir)
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
    if mode != "folder":
        raise PyxbuildError(f"mode {mode!r} is not implemented yet",
                            hint="use mode = \"folder\" (the default)")
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
            launchers, report = assemble(cfg, tb, staging, opts)
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
            app_dir = dist / f"{cfg.normalized_name}-{target.name}"
            if app_dir.exists():
                shutil.rmtree(app_dir)
            os.replace(staging, app_dir)
        launchers = [app_dir / p.name for p in launchers]
        zip_path = None
        if make_zip:
            zip_path = dist / f"{cfg.normalized_name}-{tb.project_wheel.version}-{target.name}.zip"
            write_zip(app_dir, zip_path, source_date_epoch())
        _log(opts, f"[{target.name}] built {app_dir}")
        results.append(BuildResult(target.name, tb.runtime.spec.version, app_dir, launchers,
                                   zip_path, report))
    return results


def lock_only(cfg: ProjectConfig, opts: BuildOptions) -> list[Path]:
    opts.relock = True
    paths = []
    for target_name in opts.targets or cfg.targets:
        target = get_target(target_name)
        with tempfile.TemporaryDirectory(prefix="pyxbuild-") as tmp:
            tb = prepare(cfg, target, opts, Path(tmp), download=False)
        paths.append(tb.lock_path)
    return paths


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
                tb = prepare(scratch_cfg, target, opts, Path(tmp))
            except PyxbuildError as exc:
                report.add(doctor.ERROR, str(exc))
            else:
                _, report = assemble(scratch_cfg, tb, Path(tmp) / "app", opts,
                                     launchers=False)
        out.append((target_name, report))
    return out
