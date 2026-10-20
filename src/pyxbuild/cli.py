"""Command line interface: ``pyxbuild build | lock | doctor | sign | patch | inspect``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .errors import PyxbuildError


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("project", nargs="?", default=".", type=Path,
                   help="project directory containing pyproject.toml (default: .)")
    p.add_argument("--target", action="append", dest="targets", metavar="TARGET",
                   help="target platform, e.g. win_amd64 (repeatable; default: from config)")
    p.add_argument("--python", help="Python version to bundle, e.g. 3.12")
    p.add_argument("-q", "--quiet", action="store_true")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyxbuild",
        description="Build Windows x64 applications from a Python project on any OS.",
    )
    parser.add_argument("--version", action="version", version=f"pyxbuild {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="build the application")
    _add_common(b)
    b.add_argument("--mode", choices=["folder", "onefile"])
    b.add_argument("--zip", action=argparse.BooleanOptionalAction, default=None,
                   help="also write a .zip of the application folder")
    b.add_argument("--out", type=Path, help="output directory (default: dist)")
    b.add_argument("--frozen", action="store_true",
                   help="use the lock file as is; fail if it is missing or stale")
    b.add_argument("--relock", action="store_true", help="re-resolve dependencies")
    b.add_argument("--offline", action="store_true",
                   help="use only cached downloads (implies --frozen)")
    b.add_argument("--strict", action="store_true",
                   help="fail when the bundle check reports errors (missing DLLs, ...)")

    lk = sub.add_parser("lock", help="resolve dependencies for the target into pylock.toml")
    _add_common(lk)

    d = sub.add_parser("doctor", help="report dependencies and DLLs that will not work")
    _add_common(d)

    s = sub.add_parser("sign", help="Authenticode-sign launchers (osslsigncode or jsign)")
    s.add_argument("paths", nargs="+", type=Path,
                   help=".exe files, or application folders (signs their launchers)")
    s.add_argument("--cert", required=True, type=Path, help="PKCS#12 file (.pfx/.p12)")
    s.add_argument("--password-env", metavar="VAR",
                   help="environment variable holding the certificate password")
    s.add_argument("--timestamp-url", help="RFC 3161 timestamp server")
    s.add_argument("--description")
    s.add_argument("--url")

    pt = sub.add_parser("patch", help="update executable with only the changes between "
                                      "two onefile builds")
    pt.add_argument("--from", dest="base", required=True, type=Path,
                    help="the onefile .exe users have installed")
    pt.add_argument("--to", dest="target", required=True, type=Path,
                    help="the new onefile .exe")
    pt.add_argument("-o", "--output", type=Path,
                    help="patch file (default: <app>-update-<version>.exe next to --to)")
    pt.add_argument("--fetch-public", action="store_true",
                    help="download changed public wheels (URLs in the lock file) when the "
                         "patch runs instead of carrying their files")
    pt.add_argument("--lock", type=Path, default=Path("pylock.win_amd64.toml"),
                    help="lock file of the new build (for --fetch-public)")

    ins = sub.add_parser("inspect", help="show what a onefile/patch executable, application "
                                         "folder or its .zip/.tar.gz contains")
    ins.add_argument("file", type=Path,
                     help="onefile or patch executable, application folder, .zip or .tar.gz")
    what = ins.add_mutually_exclusive_group()
    what.add_argument("--list", nargs="*", metavar="GLOB", dest="list_globs",
                      help="list files with their sizes (only those matching a GLOB, "
                           "e.g. '*/mypkg/data/*'; * also matches /)")
    what.add_argument("--extract", nargs="+", metavar=("DIR", "GLOB"),
                      help="extract all files, or those matching a GLOB, into DIR")
    return parser


def _options(args: argparse.Namespace):
    from .builder import BuildOptions

    opts = BuildOptions(targets=args.targets, python=args.python, quiet=args.quiet)
    if args.command == "build":
        opts.mode = args.mode
        opts.zip = args.zip
        opts.out_dir = args.out
        opts.frozen = args.frozen or args.offline
        opts.offline = args.offline
        opts.relock = args.relock
        opts.strict = args.strict
    return opts


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except PyxbuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _run(args: argparse.Namespace) -> int:
    from . import builder
    from .config import load_config
    from .targets import SUPPORTED_PYTHONS

    if args.command == "sign":
        return _sign(args)
    if args.command == "patch":
        return _patch(args)
    if args.command == "inspect":
        return _inspect(args)
    cfg = load_config(args.project)
    if args.python and args.python not in SUPPORTED_PYTHONS:
        raise PyxbuildError(f"unsupported --python {args.python}",
                            hint=f"supported: {', '.join(SUPPORTED_PYTHONS)}")
    opts = _options(args)
    if args.command == "build":
        for r in builder.build(cfg, opts):
            print(f"{r.target}: {r.app_dir}")
            for exe in r.launchers:
                print(f"  {exe.name}")
            if r.zip_path:
                print(f"  zip: {r.zip_path}")
        return 0
    if args.command == "lock":
        for path in builder.lock_only(cfg, opts):
            print(path)
        return 0
    if args.command == "doctor":
        status = 0
        for target, report in builder.check(cfg, opts):
            print(f"== {target} ==")
            print(report.format())
            status |= bool(report.errors)
        return status
    raise AssertionError(args.command)


def _patch(args: argparse.Namespace) -> int:
    import json

    from .patch import _onefile, make_patch, public_wheels

    wheels = None
    if args.fetch_public:
        if not args.lock.is_file():
            raise PyxbuildError(f"lock file {args.lock} not found",
                                hint="pass --lock with the new build's pylock file")
        _, files = _onefile(args.target)
        python = json.loads(files["runtime/pyxbuild.json"])["python"]
        wheels = public_wheels(args.lock, python)
    out = args.output
    if out is None:
        manifest, files = _onefile(args.target)
        version = json.loads(files["runtime/pyxbuild.json"])["project"]["version"]
        out = args.target.with_name(f"{manifest['app']}-update-{version}.exe")
    info = make_patch(args.base, args.target, out, wheels=wheels)
    print(f"{info.path} ({info.size / 1024:.0f} KB): {info.app} {info.version}; "
          f"{info.reused} file(s) reused, {info.carried} carried "
          f"({info.carried_bytes / 1024:.0f} KB), {info.fetched} downloaded when applied")
    return 0


def _inspect(args: argparse.Namespace) -> int:
    from .inspector import extract, listing, open_contents, summary

    contents = open_contents(args.file)
    if args.extract:
        dest, *globs = args.extract
        n = extract(contents, Path(dest), globs)
        if n == 0 and globs:
            raise PyxbuildError(f"no file matches {' '.join(globs)}",
                                hint="see the paths with --list")
        print(f"extracted {n} file(s) to {dest}")
    elif args.list_globs is not None:
        print("\n".join(listing(contents, args.list_globs)))
    else:
        print("\n".join(summary(contents)))
    return 0


def _sign(args: argparse.Namespace) -> int:
    from .sign import launchers_in, sign_file

    password = None
    if args.password_env:
        password = os.environ.get(args.password_env)
        if password is None:
            raise PyxbuildError(f"environment variable {args.password_env} is not set")
    files: list[Path] = []
    for p in args.paths:
        files.extend(launchers_in(p) if p.is_dir() else [p])
    if not files:
        raise PyxbuildError("nothing to sign")
    for f in files:
        sign_file(f, args.cert, password=password, timestamp_url=args.timestamp_url,
                  description=args.description, url=args.url)
        print(f"signed {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
