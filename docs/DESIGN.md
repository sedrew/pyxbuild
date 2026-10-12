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

## Environment variables

| variable | effect |
|---|---|
| `PYXBUILD_CACHE_DIR` | download/runtime cache (default: platform cache dir) |
| `PYXBUILD_OFFLINE` | never download; fail if something is not cached |
| `PYXBUILD_RUNTIME_MIRROR` | base URL instead of GitHub releases |
| `PYXBUILD_UV` | uv executable to use |
| `SOURCE_DATE_EPOCH` | timestamps written to files and the zip (default 1980-01-01) |
