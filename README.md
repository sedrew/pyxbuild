# pyxbuild

Build a Windows x64 application from a standard Python project — on macOS,
Linux or Windows. No compiler, no Wine, no virtual machine. Linux x86_64 and
aarch64 applications are built the same way, from any of the three.

```console
$ pyxbuild build
[win_amd64] Python 3.12 runtime (python-build-standalone)
[win_amd64] building wheel for myapp
[win_amd64] resolving 2 requirement(s) with uv
[win_amd64] installing 14 wheel(s)
[win_amd64] built dist/myapp-win_amd64
```

```
dist/myapp-win_amd64/
├── myapp.exe            ← launcher (one per entry point)
└── runtime/             ← CPython for Windows + your wheels in Lib/site-packages
```

Copy the folder (or the `--zip`) to any Windows 10/11 x64 machine; no Python
or Visual C++ Redistributable needs to be installed.

**[Try it in the browser](https://sedrew.github.io/pyxbuild/try/)**: edit a
small project and download the `.exe`, from any device (Android and iPad
included), nothing to install.

## How it works

Building an `.exe` (or a Linux executable) here means *assembling*, not compiling:

1. download a prebuilt CPython for the target ([python-build-standalone],
   pinned by version and sha256);
2. resolve dependencies **for the target** (e.g. `sys_platform == "win32"`,
   `platform_machine == "AMD64"`) with `uv pip compile --python-platform`,
   written to a standard `pylock.toml` (PEP 751);
3. install the target's wheels (`win_amd64`, `manylinux_*`) into the runtime's
   `site-packages` with the PyPA `installer` library — no code from the wheels
   is run;
4. copy a small prebuilt launcher (C, compiled once with `zig cc` and
   shipped inside pyxbuild: PE for Windows, static ELF for Linux) and append a
   manifest naming the entry point.

The result is byte-for-byte the same on every build host.

## Installation

```console
pip install pyxbuild          # or: uv tool install pyxbuild / pipx install pyxbuild
```

Python 3.11+ on Windows, macOS or Linux.

## Usage

```toml
[project]
name = "myapp"
version = "1.2.0"
requires-python = ">=3.12"
dependencies = ["requests>=2.32", "PySide6"]

[project.gui-scripts]
myapp = "myapp.main:run"

[tool.pyxbuild]            # everything is optional
targets = ["win_amd64"]   # also "linux_x86_64", "linux_aarch64"
python = "3.12"            # default: newest supported allowed by requires-python
mode = "folder"            # or "onefile": a single .exe (see below)
lock = "pylock.toml"       # default: pylock.win_amd64.toml
exclude = ["tests/", "*.pyi"]
require = ["mypkg/data/*.yaml"]  # fail the build if no such file is installed
zip = true
utf8-mode = false          # run the app with -X utf8
icon = "assets/app.ico"    # launcher icon (or a list of PNGs); VERSIONINFO from [project]
include = ["assets/**", "LICENSE"]  # project files copied next to the app ({app_dir})
find-links = ["wheels/"]   # extra local wheels (e.g. built in CI)
slim = ["tests", "idle", "ensurepip", "turtledemo", "lib2to3", "tk"]  # drop stdlib parts
bytecode = true            # precompiled .pyc: fast first start, read-only folders
bytecode-optimize = 2      # .pyc without asserts (1) and docstrings (2)
strip-sources = true       # ship your own modules as .pyc only (implies bytecode)
pure-python = ["msgpack"]  # build without the optional C extension (no wheel case)
cross-compile = ["markupsafe"]  # experimental: compile C extensions with zig
app-id = "Acme.MyApp"      # GUI: taskbar identity (AppUserModelID)
truststore = true          # use the system certificate store for TLS
gui-log = "{data_dir}/logs/%Y-%m-%d.log"  # GUI apps' stdout/stderr; false: none

[tool.pyxbuild.env]        # set at start-up; {app_dir} {data_dir} {localappdata}
NLTK_DATA = "{app_dir}\\nltk_data"

[tool.pyxbuild.version-info]
company-name = "ACME"
copyright = "(c) 2026 ACME"

[tool.pyxbuild.build-info]  # stored in runtime/pyxbuild.json for the app to read
channel = "beta"
```

```console
pyxbuild build [--target win_amd64|linux_x86_64|linux_aarch64] [--python 3.12] [--zip] [--frozen | --offline]
pyxbuild lock                    # resolve only, (re)write pylock.win_amd64.toml
pyxbuild doctor                  # missing wheels, missing DLLs, VC++ runtime, long paths
pyxbuild sign dist/myapp-win_amd64 --cert cert.pfx --password-env PFX_PASSWORD
pyxbuild inspect dist/myapp.exe --list '*/mypkg/data/*'   # what is inside a build
```

Commit the lock file: `pyxbuild build --frozen` then never resolves again, and
`--offline` builds from the download cache only.

### Assets, sources and build info

* `include` copies project files that are not part of the package (game
  assets, `LICENSE`, config templates) to the same relative path in the
  application folder; read them through `{app_dir}`, e.g.
  `[tool.pyxbuild.env] GAME_ASSETS = "{app_dir}/assets"`. Patterns are
  gitignore-style and relative to the project folder; `.git`, virtual
  environments and the output folder are skipped.
* `strip-sources = true` keeps only `.pyc` files for your own modules
  (dependencies keep their sources). It hides the code from a casual look and
  nothing more: `.pyc` files decompile. `bytecode-optimize = 2` also drops
  asserts and docstrings.
* `slim` also knows engine presets that drop files an application never uses:
  `panda3d-tools` (Panda3D's model converters, ~57 MB) and `arcade-assets`
  (Arcade's sample `:resources:` images, sounds and examples, ~18 MB; its
  fonts and shaders stay). macOS `.dylib` files that some wheels carry are
  never installed for Windows or Linux (Arcade: 59 MB of ffmpeg).
* `icon` takes an `.ico` or a list of PNG files (each up to 256×256; give 16,
  32, 48 and 256 px), stored in the launcher as they are.
* `require` lists files the application must contain (patterns relative to
  site-packages, like `exclude`): the build fails, naming the empty pattern
  and the wheel the file was expected from, instead of the app failing on the
  user's machine. `pyxbuild inspect` shows what a onefile/patch executable,
  folder or `.zip` contains (`--list`, `--extract`); see
  [inspecting builds](https://github.com/sedrew/pyxbuild/blob/main/docs/INSPECT.md).
* `[tool.pyxbuild.build-info]` is stored as `"build-info"` in
  `runtime/pyxbuild.json` (`os.path.join(sys.prefix, "pyxbuild.json")`), e.g.
  a release channel or commit for an About box.

### Linux applications

`--target linux_x86_64` / `--target linux_aarch64` (or `targets = [...]`)
build the same project for Linux, from macOS, Linux or Windows:

```
dist/myapp-linux_x86_64/
├── myapp              ← launcher (static ELF, one per entry point)
└── runtime/           ← CPython for Linux + your wheels in lib/python3.X/site-packages
```

* Runs on glibc 2.28 or newer (RHEL/Rocky 8, Debian 10, Ubuntu 20.04 and later);
  wheels are chosen from `manylinux_2_28` down to `manylinux1`.
* `zip = true` writes a `.tar.gz` (it keeps executable bits); `mode = "onefile"`
  gives one executable that extracts once to `~/.cache/pyx/<app>/<hash>`
  (`$PYX_ONEFILE_DIR`, `$XDG_CACHE_HOME` respected; `onefile-extract = "temp"`
  uses `$TMPDIR` and cleans up). Concurrent first starts extract once, and old
  versions are removed unless a copy is still running.
* Console scripts of dependencies (`runtime/bin/*`) are relocatable shell/Python
  scripts, so the folder can be moved anywhere.
* `{data_dir}` is `$XDG_DATA_HOME/<app>` (default `~/.local/share/<app>`).
* Windows-only, ignored with a warning for Linux targets: `icon`,
  `version-info`, `app-id`, `onefile-progress`, `cross-compile`; also not for
  Linux: the error message box, patch executables, signing and the DLL check.

### In the browser

The [sandbox](https://sedrew.github.io/pyxbuild/try/) runs the real
pyxbuild in [Pyodide](https://pyodide.org) inside a web worker: no processes
there, so uv is replaced by a built-in resolver (resolvelib over PyPI's JSON
API and PEP 658 metadata; same versions as uv in our checks) and the project
wheel is packed in-process (static `[project]` metadata, `src/<pkg>`,
`<pkg>/` or `<module>.py`). Wheels and the Windows runtime are downloaded by
the browser; the runtimes are served by the site because GitHub release
downloads do not allow cross-origin requests. The built-in resolver also works
on the command line: `PYXBUILD_RESOLVER=python pyxbuild build`.

### GitHub Actions

Build on any runner and get the application as a workflow artifact:

```yaml
on: [push, workflow_dispatch]   # workflow_dispatch: a "Run workflow" button
jobs:
  windows-app:
    runs-on: ubuntu-latest   # or macos-latest, windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: sedrew/pyxbuild@main
        with:
          project: .                 # folder with pyproject.toml
          args: --mode onefile       # any `pyxbuild build` options
          artifact-name: windows-app
```

The action installs pyxbuild (`pyxbuild-version`, default: the action's own
commit), caches the downloaded runtime and wheels (`cache: true`), builds into
a temporary folder (output `dist`) and uploads it (`upload: true`). Edit code
and press "Run workflow" from the GitHub website or app, on any device, and
download a ready `.exe`.

### One file

`mode = "onefile"` (or `pyxbuild build --mode onefile`) produces
`dist/<script>.exe` alone: the application folder travels inside the
executable as a compressed archive. Windows can only load DLLs and `.pyd`
files from disk, so the launcher extracts it:

* `onefile-extract = "cache"` (default): once per version, into
  `%LOCALAPPDATA%\pyx\<app>\<hash>\`; later starts reuse it and are as fast
  as the folder mode. Extraction goes to a temporary folder that is renamed
  when complete, under a named mutex, so concurrent or interrupted first
  starts are safe; the previous version is kept, older ones are removed.
  `PYX_ONEFILE_DIR` changes the location, `PYX_ONEFILE_RESET=1` re-extracts.
* `onefile-extract = "temp"`: like PyInstaller, into `%TEMP%` on every start,
  deleted when the application exits (folders of killed runs are swept on a
  later start).

GUI apps show a small progress window while the first extraction runs
(only if it takes longer than 0.4 s; `PYX_ONEFILE_PROGRESS=0` turns it off).
By default it is drawn in the Windows 11 style: the app's icon and name
(`version-info` `product-name`, else the project name), the user's accent
color, light or dark like Windows, sharp on scaled displays, rounded corners
on Windows 11; it never takes the focus. `onefile-progress = "classic"` shows
a plain box with the percentage instead, `"off"` nothing. Under Wine:

![Progress window of a onefile GUI app](https://raw.githubusercontent.com/sedrew/pyxbuild/main/docs/images/onefile-progress.png)

Combine it with `bytecode = true` for a fast first start. `{app_dir}` in
`[tool.pyxbuild.env]` is the extracted folder (bundled data), `{exe_dir}` the
folder of the `.exe`. `pyxbuild sign dist/myapp.exe` signs it, archive
included. The launcher stays free of the C runtime (19 KB, 27 KB with the GUI window; its own DEFLATE
decoder).

### Updates (patch executables)

Users of a onefile app (cache mode) do not need to download the whole new
executable again:

```
pyxbuild patch --from v1/myapp.exe --to dist/myapp.exe     # -> myapp-update-2.0.exe
```

writes a small executable with only what changed: files whose content the
installed version already has (at any path) are reused, the rest travels in
the patch. With `--fetch-public` the files of public wheels from the lock file
(`--lock`, default `pylock.win_amd64.toml`) are not carried; the patch
downloads those wheels from their URLs when it runs and checks their sha256.
Your own code and private/local wheels always travel inside the patch.

Running the patch builds the new version next to the installed one (reused
files are hard-linked after checking their hashes; every written file is
checked), with the installed version's Python, so the result is the folder
the new full executable would extract. The old `myapp.exe` then starts the
new version, and the new `myapp.exe` finds it ready. A patch for a version
that is not installed on that computer says so and changes nothing. A change
of only your code makes a patch of a few tens of KB.

### Monorepos

Dependencies on sibling packages through uv's `[tool.uv.sources]` work:
`{ workspace = true }` (members of the uv workspace above the project) and
`{ path = "../tools" }` (a folder or a `.whl`). pyxbuild builds those packages
from the checkout like the project itself, installs them next to it and
resolves only their dependencies, so the lock never names them and stays
portable. `directory` entries of a PEP 751 lock exported by another tool
(`uv export --format pylock.toml`) are built the same way. Local packages must
be pure Python (a package with compiled code: build its `win_amd64` wheel in
CI and add it through `find-links`).

Files found through `find-links` are written to the lock as paths relative to
the lock file, also above the project (`find-links = ["../../wheels"]` →
`path = "../../wheels/x.whl"`), so `--frozen` works on any checkout with the
same layout. Only a folder on another Windows drive is kept as an absolute
`file://` URL.

## Limits

* Every dependency needs a wheel for the target (`win_amd64` or
  `manylinux_*`, `abi3` included) or a pure-Python one on the index, or one of
  the fallbacks in [docs/NO_WHEEL.md](https://github.com/sedrew/pyxbuild/blob/main/docs/NO_WHEEL.md)
  (`pure-python`, Windows-CI wheels via `find-links`, experimental
  `cross-compile` with zig — 14 of 19 tested sdists build that way).
* The project itself must be pure Python.
* Some wheels need Visual C++ runtime DLLs that Windows does not ship
  (torch, ...). `pyxbuild doctor` lists them; add `msvc-runtime` to the
  dependencies to bundle them.
* Targets: `win_amd64`, `linux_x86_64`, `linux_aarch64` (glibc 2.28+). No
  macOS yet; the Windows-only features are listed under
  [Linux applications](#linux-applications).

Documentation: [design](https://github.com/sedrew/pyxbuild/blob/main/docs/DESIGN.md) ·
[inspecting builds and required files](https://github.com/sedrew/pyxbuild/blob/main/docs/INSPECT.md) · [testing](https://github.com/sedrew/pyxbuild/blob/main/docs/TESTING.md) ·
[compatibility matrix](https://github.com/sedrew/pyxbuild/blob/main/docs/COMPATIBILITY.md) (38 packages × Python 3.11–3.14) ·
[real applications](https://github.com/sedrew/pyxbuild/blob/main/docs/APPS.md) (Django, FastAPI, Streamlit, JupyterLab, zig cross-compiled extensions, … — 22 of 23 work) ·
[no wheel for the target?](https://github.com/sedrew/pyxbuild/blob/main/docs/NO_WHEEL.md) ·
[real-world issues and coverage](https://github.com/sedrew/pyxbuild/blob/main/docs/REAL_WORLD_ISSUES.md) ·
[benchmarks vs PyInstaller](https://github.com/sedrew/pyxbuild/blob/main/docs/BENCHMARKS.md) ·
[Go / TypeScript comparison](https://github.com/sedrew/pyxbuild/blob/main/docs/GO_TYPESCRIPT.md) ·
[open questions](https://github.com/sedrew/pyxbuild/blob/main/docs/OPEN_QUESTIONS.md).

`pyxbuild doctor` also checks the project's own imports (undeclared
dependencies, POSIX-only modules such as `fcntl`), DLLs and the VC++ runtime,
several OpenMP runtimes, packages held back for lack of wheels, and packages
known to download data or compile at run time.

[python-build-standalone]: https://github.com/astral-sh/python-build-standalone

## Development

```console
pip install -e ".[dev,stubs]"
pytest -m "not slow"                       # unit tests, seconds
eval "$(tools/get_wine.sh)"                # Wine 11 for running apps on Linux
pytest -m "slow and not golden"            # build + run real apps
pytest -m golden                           # golden set (numpy, pandas, Qt, ...)
python tools/build_stubs.py --check        # committed launchers == source
```
