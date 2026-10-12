# Design

## Pipeline

```
pyproject.toml ─► config ─► runtime (PBS, pinned sha256)
                     │
                     ├─► project wheel (uv build, must be py3-none-any)
                     │        │ Requires-Dist, markers evaluated for the TARGET
                     │        ▼
                     ├─► uv pip compile --python-platform x86_64-pc-windows-msvc
                     │        ▼
                     │   pylock.win_amd64.toml  (PEP 751, + environments, + [tool.pyxbuild])
                     │        ▼ select best wheel per package by PEP 425 tag priority
                     ├─► download (content-addressed cache, sha256 verified)
                     ▼
            dist/<name>-win_amd64/
              runtime/            copy of the runtime minus include/, libs/, pip
                Lib/site-packages wheels installed with `installer` (nt scheme)
                Scripts/          console scripts of dependencies (pyxbuild launchers)
                pyxbuild.json     build description
              <script>.exe        launcher per [project.scripts]/[project.gui-scripts]
                     ▼
            doctor: PE imports of every .pyd/.dll, VC++ runtime, path lengths
```

Modules: `config.py`, `targets.py` (markers, tags), `runtime.py`, `lock.py`,
`download.py`, `project.py`, `install.py`, `launcher.py`, `pe.py`,
`doctor.py`, `sign.py`, `builder.py`, `cli.py`.

## Decisions

* **Install into the runtime's own `site-packages`** (not a separate `lib/`):
  `site` processes `.pth` files (pywin32), `importlib.metadata` and
  `pip list` work, `sys.prefix` is normal, and no `sys.path` patching is
  needed.
* **The launcher is a 7 KB C program without a C runtime** (only KERNEL32
  and USER32; own entry point). The child is created suspended, put into a
  job object, then resumed; the GUI variant clears Explorer's busy cursor.
* **Start-up shim** `runtime/Lib/_pyxbuild_boot.py` (application launchers
  only): reads `pyxbuild.json`, removes foreign `TCL_LIBRARY`/`QT_*`
  variables, sets `[tool.pyxbuild.env]`, gives GUI apps a log file and an
  error dialog, AppUserModelID, DPI awareness, optional truststore, then
  calls the entry point.
* **Windowless processes get standard streams**: `_pyxbuild_streams.pth`
  in site-packages imports `runtime/Lib/_pyxbuild_streams.py` at the start of
  every interpreter of the bundle. In `pythonw.exe` processes, the
  multiprocessing and `ProcessPoolExecutor` children included (they never run
  the start-up shim), `sys.stdout`/`sys.stderr` would be None; they get the
  GUI log the main process opened (`PYXBUILD_GUI_LOG`), or devnull.
* **Launcher spawns `runtime\python.exe`** as a child process in a job
  object (like the pip/distlib and uv launchers), instead of embedding
  `python3XX.dll`. Consequences: `sys.executable` is a real interpreter, so
  `multiprocessing` and `subprocess.run([sys.executable, ...])` work; exit
  codes are forwarded; Ctrl+C / Ctrl+Break go to the child; killing the
  launcher kills the app. Cost: two processes.
* **Interpreter flags `-I`**: the app ignores `PYTHONPATH`/`PYTHONHOME`, user
  site-packages and the current directory. Side effect: `PYTHONUTF8` and
  `PYTHONIOENCODING` are ignored too, hence the `utf8-mode` option.
* **RECORD paths are host-independent**: `installer` writes absolute paths
  for `Scripts/` when it runs on Windows; pyxbuild always writes
  `../../Scripts/x.exe` (what pip writes), so the bundle is identical on all
  hosts.
* **Windows file-system rules on every host**: the installer maps every
  path onto the spelling that already exists, compared case-insensitively
  like NTFS (`Crypto/` and `crypto/` become one folder, as on Windows and
  macOS); a file that already exists may be written again only with
  identical bytes; names Windows cannot store (`aux.py`, `a:b`, trailing
  dot) are refused unless `exclude` drops them, and excluded files are never
  written. RECORD keeps each wheel's own spelling, as pip writes it.
* **Local packages** (`local.py`): uv workspace members and `path` sources
  named in `[tool.uv.sources]` are built from the checkout like the project
  wheel and installed with it; the resolver only sees their dependencies, so
  the lock file never contains machine-specific local paths for them.
* **Console scripts of dependencies** use the pyxbuild launcher with
  `python=..\python.exe` instead of distlib launchers, which need an
  absolute interpreter path.
* **pip is removed** from the bundled runtime.
* **Bytecode** is optional (`bytecode = true`): a host CPython of the same
  X.Y (provided by uv) writes unchecked-hash `.pyc` with `/`-separated
  embedded paths, so builds stay reproducible across hosts.
* **No-wheel fallbacks** (`fallbacks.py`, `crossbuild.py`): see
  [NO_WHEEL.md](NO_WHEEL.md).
* **Runtime pinning**: `src/pyxbuild/runtimes.json` lists one
  python-build-standalone release with sha256 per Python version, generated
  by `tools/update_runtimes.py <release>`. `PYXBUILD_RUNTIME_MIRROR` overrides
  the download base URL.

## Launcher manifest (format 1)

```
[stub PE][payload][u32 LE payload length][u32 LE version = 1][8 bytes "PYXBLD01"]
```

Payload: UTF-8, one `key=value` per line.

| key | meaning |
|---|---|
| `python` | interpreter, relative to the launcher directory (`runtime\python.exe`, `..\pythonw.exe`) |
| `flags` | interpreter flags before `-c` (`-I`, `-I -X utf8`) |
| `code` | one-line program for `-c`; never contains `"` |
| `entry-point` | informational `module:attr` |

The launcher runs `"<dir>\<python>" <flags> -c "<code>" "<launcher path>" <original arguments>`;
the bootstrap code moves the launcher path into `sys.argv[0]`.

When the file is Authenticode-signed, the certificate table follows the
trailer; the launcher finds the original end of file from the PE security
directory and skips the ≤7 zero bytes of alignment padding.

## Onefile (format 1)

```
[onefile stub (+ resources)][archive][manifest][trailer]
```

The archive is described in `src/pyxbuild/onefile.py`: a header, a chunk
table, and the stream of `(path, size, data)` records cut into 1 MiB chunks
that are raw DEFLATE (or stored when that does not help), compressed in
parallel and deterministically. The launcher needs two chunk-sized buffers;
`stub/inflate.h` is its DEFLATE decoder (tested against zlib, also on
corrupted input). Extra manifest keys: `onefile` (archive sha256),
`app`, `archive-offset`, `archive-size`, `extract` (`cache`/`temp`); `python`
is then relative to the extracted folder.

Cache mode: `<base>\<app>\<first 12 hex of the hash>\` with base
`%LOCALAPPDATA%\pyx` or `$PYX_ONEFILE_DIR`. A start that finds
`.pyx-complete` runs at once. Otherwise, under the mutex
`Local\pyx-onefile-<hash12>`, it extracts to `<hash12>.tmp-<pid>-<tick>`
(`\\?\` paths, no MAX_PATH limit while extracting), writes the marker and
renames the folder into place; then it removes the other versions except the
newest one, and abandoned `.tmp-`/trash folders older than ten minutes.
Folders are renamed to `.pyx-trash-*` before deletion, which Windows refuses
while a program runs from them.

GUI launchers show a window ("Preparing <app> for its first start… N%")
when an extraction runs longer than 0.4 s, pumping messages between chunks;
no window when there is no desktop. Manifest key `progress` (absent =
modern): the modern window is a `#32770` dialog with a `msctls_progress32`
bar; comctl32 is loaded at run time inside the activation context of
shell32.dll's manifest (resource 124), so the bar gets the visual styles of
comctl32 v6 without a manifest or an import in the launcher; the process
becomes system-DPI aware and uses the message font from
`SPI_GETNONCLIENTMETRICS`. `classic` is a single STATIC window, `off` none. `PYX_ONEFILE_PROGRESS`:
`0` off, `1` at once.

Temp mode: `%TEMP%\pyx-<app>-<pid>-<tick>\`, deleted after the child exits;
our folders older than ten minutes are swept at start.

Patch executables (`src/pyxbuild/patch.py`): the onefile console stub with
an archive holding `patch.json`, `blobs/<sha256>` and
`_pyxbuild_patch.py`, and the extra manifest key `patch-base` (archive hash
of the version it applies to). The launcher refuses to run unless
`<base>\<app>\<base12>\.pyx-complete` exists, extracts the payload to
`%TEMP%`, and runs the base version's `runtime\python.exe` with
`PYX_PATCH_PAYLOAD`, `PYX_PATCH_APPDIR`, `PYX_PATCH_EXE`. `patch.json`
(format 1) lists `keep` (paths whose content is unchanged, checked together
by `keep_sha256`, the sha256 of their concatenated hex digests), `wheels`
(url, sha256, size) and `files` for everything else, each from `base`
(another path of the old version), `payload` or `wheel` (index, member).
The new version is assembled in `<target12>.tmp-<pid>-patch` under the
target's mutex and renamed into place; then `.pyx-next` in the base folder
(and in folders that pointed at the base) names `<target12>`. A cache-mode
start follows `.pyx-next` (up to 16 hops) to a complete folder, so older
executables run the newest installed version. Applying a patch removes no
versions; the next extraction by a full executable sweeps as usual (an old
executable whose folder was swept extracts itself again).

## Environment variables

| variable | effect |
|---|---|
| `PYXBUILD_CACHE_DIR` | download/runtime cache (default: platform cache dir) |
| `PYXBUILD_OFFLINE` | never download; fail if something is not cached |
| `PYXBUILD_RUNTIME_MIRROR` | base URL instead of GitHub releases |
| `PYXBUILD_UV` | uv executable to use |
| `PYX_ONEFILE_DIR` | (end user) where onefile apps are extracted; default `%LOCALAPPDATA%\pyx` |
| `PYX_ONEFILE_RESET` | (end user) `1`: extract a onefile app again |
| `SOURCE_DATE_EPOCH` | timestamps written to files and the zip (default 1980-01-01) |
