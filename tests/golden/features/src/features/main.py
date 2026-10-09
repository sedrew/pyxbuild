"""Exercises runtime features a frozen app depends on; prints OK/FAIL per check."""

import os
import sys
import traceback


def _child(q):
    q.put(os.getpid())


def check_multiprocessing():
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=_child, args=(q,))
    p.start()
    pid = q.get(timeout=120)
    p.join(120)
    assert p.exitcode == 0 and pid != os.getpid()


def check_subprocess_python():
    import subprocess

    out = subprocess.check_output([sys.executable, "-c", "import sys; print(sys.prefix)"],
                                  text=True)
    assert out.strip() == sys.prefix, (out, sys.prefix)


def check_resources():
    from importlib.resources import files

    text = files("features").joinpath("data/greeting.txt").read_text()
    assert text.strip() == "hello from package data"
    assert not files("features").joinpath("typing_only.pyi").is_file()  # excluded


def check_metadata():
    from importlib.metadata import distribution, version

    assert version("features") == "0.3.0"
    assert distribution("features").read_text("INSTALLER").strip() == "pyxbuild"


def check_stdlib_extensions():
    import ctypes
    import hashlib
    import lzma
    import sqlite3
    import ssl
    import zlib

    con = sqlite3.connect(":memory:")
    assert con.execute("select 1+1").fetchone() == (2,)
    assert ssl.OPENSSL_VERSION
    assert hashlib.sha3_256(b"x").hexdigest()
    assert lzma.decompress(lzma.compress(b"abc")) == b"abc"
    assert zlib.decompress(zlib.compress(b"abc")) == b"abc"
    assert ctypes.sizeof(ctypes.c_void_p) == 8


def check_tcl():
    import tkinter

    tcl = tkinter.Tcl()
    assert tcl.eval("expr {6*7}") == "42"


def check_isolation():
    assert sys.flags.isolated == 1
    assert "" not in sys.path and os.getcwd() not in sys.path
    assert sys.platform == "win32"


def check_utf8_mode():
    assert sys.flags.utf8_mode == 1
    assert sys.stdout is None or sys.stdout.encoding == "utf-8"


def check_argv():
    assert sys.argv[0].lower().endswith(("features.exe", "features-gui.exe")), sys.argv


CHECKS = [check_argv, check_utf8_mode, check_isolation, check_metadata, check_resources,
          check_stdlib_extensions, check_tcl, check_subprocess_python, check_multiprocessing]


def _run_checks(out):
    failed = 0
    for check in CHECKS:
        name = check.__name__.removeprefix("check_")
        try:
            check()
            print(f"OK {name}", file=out)
        except Exception:
            failed += 1
            print(f"FAIL {name}: {traceback.format_exc()}", file=out)
    return failed


def run():
    args = sys.argv[1:]
    if args[:1] == ["--echo"]:
        print("ARGS " + repr(args[1:]))
        return 0
    if args[:1] == ["--exit"]:
        return int(args[1])
    if args[:1] == ["--raise"]:
        raise RuntimeError("boom")
    if args[:1] == ["--wait-break"]:
        import signal
        import time

        def on_break(signum, frame):
            print("GOT SIGBREAK", flush=True)
            sys.exit(42)

        signal.signal(signal.SIGBREAK, on_break)
        print("READY", flush=True)
        # short sleeps: on Windows only SIGINT interrupts time.sleep, so a
        # SIGBREAK handler runs when the current sleep returns
        for _ in range(600):
            time.sleep(0.1)
        return 1
    return 1 if _run_checks(sys.stdout) else 0


def gui():
    """GUI subsystem: no console, so results go to the file named in argv[1]."""
    with open(sys.argv[1], "w", encoding="utf-8") as out:
        print(f"stdout is None: {sys.stdout is None}", file=out)
        failed = _run_checks(out)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
