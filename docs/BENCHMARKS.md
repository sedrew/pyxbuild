# Benchmarks

`tools/bench.py`, run by the CI job "benchmark vs PyInstaller" on GitHub's
`windows-latest` (October 2026, Python 3.12, 7 runs per start-up figure).
PyInstaller 6 builds the same project with `--onedir` from a venv on the same
machine (pip install time not counted). Start-up = run the entry point to
completion: `hello` imports requests; `level1` runs the numpy smoke check.

## Start-up (seconds)

| app | pyxbuild default | pyxbuild `bytecode = true` | PyInstaller onedir |
|---|---|---|---|
| hello — first run after build | 0.99 | **0.24** | 0.35 |
| hello — median | **0.21** | **0.21** | 0.31 |
| level1 — first run after build | 5.07 | **0.13** | 0.29 |
| level1 — median | **0.12** | **0.11** | 0.26 |
| bundled `python.exe -c pass` | 0.02–0.03 | | |

* The launcher adds no measurable time (measured under Wine:
  `hello.exe` 2.079 s vs `python.exe -I -c "<same code>"` 2.079 s).
* Without precompiled bytecode the **first** start compiles every imported
  module (5 s for numpy + pandas) and needs a writable folder; with
  `bytecode = true` the first start is as fast as the rest.
* pyxbuild starts faster than PyInstaller onedir once bytecode exists: no
  bootloader unpacking/archive import layer, a plain interpreter with
  `site-packages`.

## Onefile (seconds and MB, windows-latest)

CI run [#28](https://github.com/sedrew/pyxbuild/actions/runs/38275951221),
Python 3.12, `bytecode = true` for both pyxbuild variants, 7 runs.

| app | variant | .exe | first start | later starts (median) |
|---|---|---|---|---|
| hello | pyxbuild onefile (cache) | 17.6 MB | 2.70 | **0.20** |
| hello | pyxbuild onefile, `onefile-extract = "temp"` | 17.6 MB | 3.06 | 3.20 |
| hello | PyInstaller `--onefile` | 11.5 MB | 1.08 | 1.02 |
| level1 | pyxbuild onefile (cache) | 69.6 MB | 9.8–27.5 ¹ | **0.16** |
| level1 | pyxbuild onefile, `onefile-extract = "temp"` | 69.6 MB | 11.30 | 13.78 |
| level1 | PyInstaller `--onefile` | 49.6 MB | 4.10 | 4.10 |

* With the cache, every start after the first is as fast as the folder
  mode (0.16–0.20 s); PyInstaller onefile unpacks on every start (1.0 s for
  hello, 4.1 s for numpy + pandas). Temp mode behaves like PyInstaller, only
  slower: it unpacks the whole application (stdlib and complete wheels) where
  PyInstaller unpacks only what its analysis collected.
* The first start of the cache mode pays one extraction. ¹ For level1 two
  CI runs measured 27.5 s ([#28](https://github.com/sedrew/pyxbuild/actions/runs/38275951221))
  and 9.8 s ([#29](https://github.com/sedrew/pyxbuild/actions/runs/38279385637));
  in #29 three more extractions (`PYX_ONEFILE_RESET=1`, which also deletes the
  previous copy) took 12.7 s median, against 11.5 s for temp mode. So the
  cache adds no cost of its own; 27.5 s was runner noise.
* The executable is 1.4–1.5× PyInstaller's for the same reason as the folder
  size below; `slim` and `exclude` shrink it.
* GitHub's runners have Defender real-time scanning turned off; on a normal
  machine extraction (thousands of new files) will be slower.

## Size

| app | variant | folder | files | zip (deflate) |
|---|---|---|---|---|
| hello | pyxbuild default | 44.6 MB | 1 983 | 14.8 MB |
| hello | pyxbuild `slim` + `tk`, `pydoc` | 33.1 MB | 697 | 10.6 MB |
| hello | pyxbuild `bytecode = true` | 56.5 MB | 2 601 | 19.8 MB |
| hello | PyInstaller onedir | 24.4 MB | 82 | 11.9 MB |
| level1 | pyxbuild default | 165.6 MB | 5 785 | 55.4 MB |
| level1 | pyxbuild `slim` + `tk`, `pydoc` | 154.1 MB | 4 499 | 51.2 MB |
| level1 | pyxbuild `bytecode = true` | 223.7 MB | 8 617 | 77.5 MB |
| level1 | PyInstaller onedir | 117.1 MB | 1 756 | 52.0 MB |

pyxbuild ships whole wheels and the whole standard library (correctness
over size: no hidden imports, `importlib.metadata` works). PyInstaller ships
only what its import analysis found, as bytecode in an archive. Compressed,
the difference is small (52.0 vs 51.2–55.4 MB for level1).

### Where the size goes (hello, default, 44.6 MB)

| part | size |
|---|---|
| `DLLs/` — OpenSSL 9.6 MB, Tcl/Tk 3.4 MB, sqlite 1.6 MB, unicodedata 1.1 MB, … | 18 MB |
| `Lib/` standard library | ~16 MB |
| `tcl/` Tcl/Tk library scripts | 7.1 MB |
| `python312.dll` | 6.3 MB |
| launcher | **7 KB** (was 61 KB before dropping the C runtime) |

### Ways to make it smaller

| measure | saving (hello) | trade-off |
|---|---|---|
| default `slim` (tests, idlelib, ensurepip, turtledemo, lib2to3) | ~4 MB | none for apps |
| `slim` += `"tk"` | ~11 MB | no tkinter (the import check flags it) |
| `slim` += `"pydoc"`, `"sqlite"`, `"venv"` | 0.5–2 MB each | lose that module |
| `exclude = ["*.pyi", "tests/"]` | package-dependent | type stubs / tests of dependencies |
| distribute `.tar.xz` instead of `.zip` | 14.1 → 9.4 MB (−33 %); level1 52.8 → 31.9 MB | Explorer opens only zip (Windows 11 also opens tar/7z) |
| zip with LZMA | 14.1 → 11.6 MB | Explorer cannot open LZMA zips |
| UPX-compressing DLLs/exe | — | not offered: antivirus false positives, slower start |

## Build time (seconds, windows-latest)

| | hello | level1 |
|---|---|---|
| cold (empty cache: download runtime + wheels, resolve, install) | 20.9 | 42.3 |
| warm (cache filled, resolve + install) | 11.3 | 28.6 |
| frozen (lock present) | 10.1 | 28.7 |
| PyInstaller (analysis + collect, packages already installed) | 12.6 | 30.9 |

On Linux the same builds take 7–8 s (hello). Most of the time is copying
the runtime and wheels and the PE/DLL scan; resolution with uv is under a
second. Leaving `slim` parts out while copying (instead of deleting them
afterwards) saved ~1 s per build.

## Reproduce

```console
python tools/bench.py --project hello --runs 7                 # any host (Wine on Linux)
python tools/bench.py --project level1 --runs 7 --pyinstaller  # Windows host
```
Under Wine absolute start-up numbers are dominated by Wine itself (≈2 s for
`import requests`); compare variants, not tools.
