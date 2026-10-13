# Real-world failure cases and pyxbuild coverage

Collected from issue trackers and forums (October 2026) and checked against
pyxbuild. **C** covered · **P** partially · **N** not covered. "now" marks
cases closed by the changes made after this review.

## Resolution and download

| # | Case | Source | Status |
|---|---|---|---|
| 1 | Markers evaluated for the host (`pip download --platform`) | [pip#6117](https://github.com/pypa/pip/issues/6117) | C — target marker environment, uv `--python-platform` |
| 2 | Transitive dependency markers ignored | [pip#4304](https://github.com/pypa/pip/issues/4304) | C |
| 3 | `platform_release` / `platform_version` cannot be set | [pip#9981](https://github.com/pypa/pip/issues/9981), [uv#6641](https://github.com/astral-sh/uv/issues/6641) | C now — doctor warns about such markers |
| 4 | Wheels of one release with different dependencies | [tensorflow#75415](https://github.com/tensorflow/tensorflow/issues/75415) | P — dependencies come from the selected wheel's lock entry |
| 5 | No wheels yet for a new CPython | [h5py#2476](https://github.com/h5py/h5py/issues/2476) | P — clear error; doctor reports held-back versions now |
| 6 | sdist built for the host during cross install | [uv#7957](https://github.com/astral-sh/uv/issues/7957) | C — refused; `pure-python` / `cross-compile` opt-ins |
| 7 | `--python-platform` behaviour differs between uv versions | [uv#9575](https://github.com/astral-sh/uv/issues/9575) | P — resolving uv version recorded in the lock now |
| 8 | Wrong-architecture wheel | [pdm#2464](https://github.com/pdm-project/pdm/issues/2464) | C — tag priority + doctor machine check |
| 9 | abi3 needs `python3.dll` | [PBS#55](https://github.com/astral-sh/python-build-standalone/issues/55) | C — doctor import check |

## Installing without running package code

| # | Case | Source | Status |
|---|---|---|---|
| 10 | pywin32 post-install / `.pth` | [pynsist#137](https://github.com/takluyver/pynsist/issues/137) | C — real site-packages; golden level 2 |
| 11 | pywin32 Windows services | [pywin32#2056](https://github.com/mhammond/pywin32/issues/2056) | P — untested |
| 12 | `importlib.metadata` missing in frozen apps | [pyinstaller#5814](https://github.com/pyinstaller/pyinstaller/issues/5814) | C — full dist-info |
| 13 | Hidden imports | [hooks-contrib#456](https://github.com/pyinstaller/pyinstaller-hooks-contrib/issues/456) | C — whole wheels |
| 14 | Qt platform plugin not found | [pyinstaller#5493](https://github.com/pyinstaller/pyinstaller/issues/5493) | C — golden PySide6 |
| 15 | win32com `gen_py` cache in a read-only folder | [pywin32#1140](https://github.com/mhammond/pywin32/issues/1140) | P — pywin32 falls back to %TEMP% |
| 16 | comtypes `gen` in a read-only folder | [comtypes#124](https://github.com/enthought/comtypes/issues/124) | P now — doctor known issue |
| 17 | `__pycache__` not writable | [python-dev](https://www.python.org/pipermail/python-dev/2010-March/098734.html) | C now — `bytecode = true` (tested read-only) |
| 18 | numba `cache=True` | [numba#4908](https://github.com/numba/numba/issues/4908) | P now — known issue + `env` for `NUMBA_CACHE_DIR` |
| 19 | Package runs pip at run time | [spaCy#10895](https://github.com/explosion/spaCy/discussions/10895) | N by design (pip removed) |
| 56 | Several wheels ship the same namespace `__init__.py` (`kivy_deps.*`) | found by `tools/apps_matrix.py` | C now — identical files allowed, as with pip; different content refused |
| 57 | Names that differ only by case (`Crypto/` of pycryptodome vs `crypto/`): two folders on Linux, one on Windows | pycryptodome + `crypto` from PyPI | C now — installed with Windows' case-insensitive rules on every host; clashing files refused, merged folders reported |
| 58 | Names Windows cannot store (`aux.py`, `con`, `a:b`, trailing dot/space) | Windows naming rules | C now — refused with a hint; `exclude` drops such files before they are written |
| 59 | Post-install scripts nobody runs (`pywin32_postinstall.py`) | pip behaviour | P now — doctor reports `*postinstall*` scripts |

## Windows runtime

| # | Case | Source | Status |
|---|---|---|---|
| 20 | `PATH` not used for extension DLLs (3.8+) | [openslide#101](https://github.com/openslide/openslide-python/issues/101) | C — doctor models the search order |
| 21 | VC++ runtime missing | [spaCy#5332](https://github.com/explosion/spaCy/issues/5332), [uv#18413](https://github.com/astral-sh/uv/issues/18413) | C — doctor + `msvc-runtime` |
| 22 | Older VC runtime copy loaded first | [pytorch#166628](https://github.com/pytorch/pytorch/issues/166628) | N |
| 23 | Two OpenMP runtimes (OMP: Error #15) | [avatarify#677](https://github.com/alievk/avatarify-python/issues/677) | P now — doctor warns |
| 24 | vcomp140 missing | [scikit-learn#18088](https://github.com/scikit-learn/scikit-learn/issues/18088) | C |
| 25 | Foreign DLLs found via `PATH` | [pyqt-feedstock#138](https://github.com/conda-forge/pyqt-feedstock/issues/138) | N |
| 26 | DLLs loaded dynamically | [pyinstaller#6679](https://github.com/orgs/pyinstaller/discussions/6679) | P — shipped, not checked statically |
| 27 | `PYTHONHOME`/`PYTHONPATH` leaking in | [spack#32515](https://github.com/spack/spack/issues/32515) | C — `-I` |
| 28 | `TCL_LIBRARY`, `QT_PLUGIN_PATH` of other installs | [PythonScript#379](https://github.com/bruderstein/PythonScript/issues/379) | C now — `clean-env` (default list), e2e test |
| 29 | Corporate TLS roots (certifi) | [pyinstaller#7229](https://github.com/pyinstaller/pyinstaller/issues/7229) | C now — `truststore = true` |
| 30 | Tcl `init.tcl` not found | [PBS#346](https://github.com/astral-sh/python-build-standalone/issues/346) | C — features golden 3.11–3.14 |
| 31 | tkinter 3.14 + spaces in path | [cpython#158178](https://github.com/python/cpython/issues/158178) | P |
| 32 | multiprocessing in frozen exe | [cx_Freeze#2435](https://github.com/marcelotduarte/cx_Freeze/issues/2435) | C — real python.exe |
| 33 | `dirname(sys.executable)` assumed to be the app folder | [pyinstaller#3915](https://github.com/pyinstaller/pyinstaller/issues/3915) | P — `sys.argv[0]` is the launcher; `{app_dir}` in `env` |
| 34 | `sys.stdout` is None in GUI apps | [playwright-python#1148](https://github.com/microsoft/playwright-python/issues/1148) | C now — log file under `%LOCALAPPDATA%\<app>\logs` |
| 60 | multiprocessing in a frozen app re-runs the app (`freeze_support()` forgotten: endless windows) | PyInstaller docs, recurring support question | C — `sys.executable` is a real `pythonw.exe`; tested: tkinter window + `Pool` + `ProcessPoolExecutor`, folder and onefile (`test_gui_mp.py`) |
| 61 | `sys.stdout` is None in windowless child processes (`AttributeError` on `write`) | found by `test_gui_mp.py` | C now — `_pyxbuild_streams.pth` gives every pythonw process the app's log |
| 35 | GUI app dies silently | [briefcase#825](https://github.com/beeware/briefcase/discussions/825) | C now — message box + log |
| 36 | Console windows flash from subprocesses | [pyinstaller#8420](https://github.com/orgs/pyinstaller/discussions/8420) | N (app level) |
| 37 | Blurry UI on high DPI | [cpython#119174](https://github.com/python/cpython/issues/119174) | C now — per-monitor DPI awareness for GUI scripts |
| 38 | Taskbar grouping/icon is Python's | [napari#1168](https://github.com/napari/napari/issues/1168) | P now — `app-id` sets AppUserModelID; process icon is still pythonw.exe's |
| 39 | Firewall prompt names python.exe | [spyder#11901](https://github.com/spyder-ide/spyder/issues/11901) | N |
| 40 | MAX_PATH | [pip#10045](https://github.com/pypa/pip/issues/10045) | P now — doctor gives the longest install folder that still fits (directories counted with CreateDirectory's MAX_PATH − 12); error when no folder fits |
| 41 | Non-ASCII paths | [pip#4984](https://github.com/pypa/pip/issues/4984) | C — e2e |
| 42 | cp1252 `UnicodeEncodeError` | [rut#6](https://github.com/schettino72/rut/issues/6) | P — `utf8-mode` (off by default) |
| 43 | zoneinfo without tzdata | [cpython#140039](https://github.com/python/cpython/issues/140039) | C now — import check warns |
| 44 | Proactor loop lacks `add_reader` | [aiodns#86](https://github.com/aio-libs/aiodns/issues/86) | N (app level) |
| 45 | `ctypes.util.find_library` ignores DLL dirs | [cpython#111104](https://github.com/python/cpython/issues/111104) | N (app level) |
| 46 | Resources relative to cwd | [pyinstaller#3915](https://github.com/pyinstaller/pyinstaller/issues/3915) | P |

## Distribution and trust

| # | Case | Source | Status |
|---|---|---|---|
| 47 | Antivirus flags the bootloader | [pyinstaller#5492](https://github.com/pyinstaller/pyinstaller/issues/5492) | P — 7 KB CRT-free stub with VERSIONINFO; no false-positive plan yet |
| 48 | SmartScreen on unsigned exes | [SCIRun#2742](https://github.com/SCIInstitute/SCIRun/issues/2742) | P — `sign` covers launchers |
| 49 | Smart App Control blocks unsigned `.pyd` | [scipy#25959](https://github.com/scipy/scipy/issues/25959) | N |
| 50 | Mark of the Web on zip-extracted files | [rehearsal-recorder#4](https://github.com/voronizer/rehearsal-recorder/issues/4) | N |

## Run-time downloads and compilers

| # | Case | Source | Status |
|---|---|---|---|
| 51 | playwright browsers | [playwright-python#494](https://github.com/microsoft/playwright-python/issues/494) | P — doctor; `env` for `PLAYWRIGHT_BROWSERS_PATH` |
| 52 | nltk data | [nltk#3312](https://github.com/nltk/nltk/issues/3312) | P now — known issue + `env` |
| 53 | spaCy models | [spaCy#10895](https://github.com/explosion/spaCy/discussions/10895) | P now — known issue |
| 54 | Hugging Face cache/offline | [transformers#30469](https://github.com/huggingface/transformers/issues/30469) | P now — known issue + `env` |
| 55 | torch.compile/triton/cffi need compiler + headers | [triton-windows#83](https://github.com/woct0rdho/triton-windows/issues/83) | P now — `keep-dev-files = true` + known issue |

## Still open (suggested next steps)

* Run the app under its own process image (copy `pythonw.exe` to
  `runtime\<app>.exe` and give it the app's icon, VERSIONINFO and a manifest)
  so the taskbar, Task Manager and the firewall show the app (38, 39).
* `pyxbuild sign --all`: sign the runtime and every unsigned `.pyd`/`.dll`,
  then refresh RECORD hashes (49).
* Doctor: compare versions of duplicated VC++ runtime DLLs (22).
* A CI job on Windows with Smart App Control / WDAC in evaluation mode (49).
* Warn about Mark of the Web in the launcher, or ship an installer (50).
