# Testing

pyxbuild is only correct if the application it builds on macOS/Linux runs on
Windows. The tests are layered so that most of them run anywhere in seconds,
and the claim "it works" always rests on a run on real Windows.

| Layer | Command | Where | What it proves |
|---|---|---|---|
| unit | `pytest -m "not slow"` | any OS, no network | markers/tags for the target, config, lock parsing and wheel choice, launcher manifest (incl. after signing), PE parser, installer layout + RECORD, excludes, doctor |
| stubs | `python tools/build_stubs.py --check` | Linux, macOS, Windows | the committed launcher binaries are exactly what `stub/launcher.c` compiles to with zig, on every host |
| e2e | `pytest -m "slow and not golden"` | Wine on Linux, natively on Windows | real builds from PyPI that run: argv quoting, Unicode, exit codes, exceptions, GUI subsystem, multiprocessing, `sys.executable` subprocesses, moved folder with spaces/Unicode, dependency console scripts, signed launchers, reproducibility, `--frozen`/`--offline` |
| golden | `pytest -m golden` / `tools/golden.py` | build anywhere, run on Windows | the golden set of packages below works when built on another OS |
| cross-host | CI `reproducibility` | ubuntu, macos-14, windows | the zips built on the three hosts have identical sha256 |
| pip parity | `tools/compare_with_pip.py` | CI windows-latest | the installed file tree equals `pip install` of the same wheels |
| real apps | `tools/apps_matrix.py` | build on ubuntu (x86_64) and macos-14 (ARM64), run on windows-latest (CI) or Wine | 23 applications (Django, FastAPI, Streamlit, JupyterLab, Scrapy, zig cross-compiled extensions, …) do real work inside the built exe; see [APPS.md](APPS.md) |

Ctrl+Break forwarding (`test_ctrl_break_...`) only runs on real Windows.

Further suites: `test_gui_mp.py` (a GUI app started like from Explorer, without standard handles: tkinter window, multiprocessing and ProcessPoolExecutor from the Tk main loop, output and exceptions of workers, in folder and onefile mode), `test_onefile.py` (archive format; under Wine/Windows: extract once and reuse, reset, repair of interrupted extractions, removal of old versions, three concurrent first starts, multiprocessing, temp mode and its cleanup, signed onefile), `test_inflate.py` (the launcher's DEFLATE decoder compiled for the host and compared with zlib, including truncated and corrupted input), `test_monorepo.py` (uv workspace members and path sources, find-links above the project, non-ASCII project metadata; the slow test builds a three-package workspace, runs it and rebuilds it `--frozen` from another location), `test_install_windows_fs.py` (case-insensitive layout, names Windows cannot store, excludes while installing, MAX_PATH budget, post-install scripts), `test_imports.py` (import check), `test_boot.py` (start-up
shim), `test_sdist.py` (pure-Python sdists, `pure-python` fallback with real
msgpack/PyYAML, zig cross-compilation), `test_review_regressions.py`
(security and correctness fixes from the code review: wheel path traversal,
cache file names, host ABI flags, self-referencing extras, portable locks),
`test_resources.py` (icon/VERSIONINFO validated with `pefile`, and with the
Windows version API on Windows). `tools/cross_matrix.py` tries
cross-compilation on real PyPI sdists; `tools/bench.py` measures build time,
size and start-up (see [BENCHMARKS.md](BENCHMARKS.md)).

## Wine

Wine is a convenience for fast local feedback, not the reference. Ubuntu
24.04's Wine 9.0 cannot run numpy (its `ucrtbase.dll` lacks `crealf`);
use Wine 10+:

```console
eval "$(tools/get_wine.sh)"      # portable Wine 11 (Kron4ek/Wine-Builds), sets PYXBUILD_WINE
```

Known Wine quirks: Python fails to initialise stdio when stdout/stderr are
redirected to a regular file (use pipes); there is no display, so the golden
tests skip `Tk()` windows and run Qt with `QT_QPA_PLATFORM=offscreen`.

## Golden set

| level | project | packages | runs on |
|---|---|---|---|
| — | `tests/golden/hello` | requests (HTTPS) | 3.12 |
| — | `tests/golden/features` | stdlib only: metadata, resources, sqlite/ssl/ctypes/lzma, Tcl, multiprocessing, isolation, UTF-8 mode, GUI script | 3.11–3.14 |
| 1 | `tests/golden/level1` | numpy, pandas, Pillow, requests+certifi, lxml, cryptography, pydantic, tkinter | 3.11–3.14 |
| 2 | `tests/golden/level2` | matplotlib, scipy, pywin32, psutil, orjson, pyzmq, opencv-python-headless, PySide6 | 3.12, 3.13 |
| 3 | `tests/golden/level3` | torch (CPU) + msvc-runtime; CI only (Wine lacks NUMA APIs OpenMP needs) | 3.12 |
| 3 | `tests/test_sdist.py` | pure-Python sdist (needs `allow-sdist`), sdist with C code (refused) | 3.12 |

The wider [compatibility matrix](COMPATIBILITY.md) (`tools/compat_matrix.py`)
builds one app per package for 38 packages with compiled code on Python
3.11–3.14 and runs each smoke test.

Each golden app prints `OK <check>` / `FAIL <check>: traceback` and exits
non-zero on any failure. Override the Python matrix with
`PYXBUILD_GOLDEN1_PYTHONS=3.12,3.13` / `PYXBUILD_GOLDEN2_PYTHONS=...`.

Not covered yet: a clean Windows without the VC++ Redistributable (GitHub's
windows-latest has it installed; the doctor's static check stands in), and
playwright (doctor warning only).
