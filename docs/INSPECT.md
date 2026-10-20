# Inspecting builds and required files

Two tools for the question "is my file in the application?": `pyxbuild inspect`
looks inside a finished build, and `[tool.pyxbuild] require` makes the build
fail when a file you depend on is missing, before anything ships.

## `pyxbuild inspect`

```console
pyxbuild inspect FILE                       # summary
pyxbuild inspect FILE --list [GLOB ...]     # paths and sizes
pyxbuild inspect FILE --extract DIR [GLOB ...]
```

`FILE` is any of:

* a onefile executable (`mode = "onefile"`, Windows `.exe` or Linux ELF, also
  after signing);
* a patch executable from `pyxbuild patch`;
* an application folder (`dist/myapp-win_amd64`), or its `.zip` / `.tar.gz`.

Put `FILE` first: `--list` and `--extract` take the rest of the command line
as their arguments.

Paths are those of the application folder, the same ones the onefile launcher
extracts: `runtime/Lib/site-packages/mypkg/...` on Windows,
`runtime/lib/python3.X/site-packages/mypkg/...` on Linux. A `.zip`/`.tar.gz`
is shown without its top folder.

**Summary** (no option): project name and version, target, Python, the pyxbuild
version that built it, the entry point and extraction mode of a onefile
executable, the number of files and their total size, and the size and
sha256 of the archive (for a `.zip`/`.tar.gz`: of the file). A onefile whose
archive hash differs from the one in its manifest was changed after the build;
the summary says so.

```console
$ pyxbuild inspect dist/myapp.exe
dist/myapp.exe: onefile application (win_amd64)
  project:  myapp 1.2.0
  python:   3.12.13
  pyxbuild: 0.1.0
  entry:    myapp.main:run
  extract:  cache (app 'myapp')
  files:    1662 (39.7 MB)
  archive:  12.8 MB, sha256 c287a1da92b7c1c5c2e847d73db0965a284ffed36679f08a9fdb316b8557975b
```

**`--list [GLOB ...]`**: one line per file, size in bytes and path, sorted, and
a total. With globs, only matching files. Globs are `fnmatch` patterns on the
whole path where `*` also matches `/`, so `'*/mypkg/data/*'` finds the folder
wherever site-packages is; a glob that names a folder lists everything below
it. Quote globs so the shell does not expand them.

```console
$ pyxbuild inspect dist/myapp.exe --list '*/mypkg/data/*'
 512  runtime/Lib/site-packages/mypkg/data/defaults.yaml
1830  runtime/Lib/site-packages/mypkg/data/schema.yaml
2 file(s), 2 KB
```

**`--extract DIR [GLOB ...]`**: writes all files, or the matching ones, below
`DIR` (created if needed, existing files are overwritten). ELF files and
scripts get the executable bit, as the Linux launcher does. Paths that would
leave `DIR` are refused. Nothing matching a glob is an error.

**Patch executables.** The summary names the version the patch applies to
(the base archive hash and, for patches made by a pyxbuild that has
`inspect`, its version), the version it installs, and how many files stay unchanged, are
added, replaced and removed, and where the written files come from (carried
in the patch, copied from the installed version, or from downloaded wheels
with `--fetch-public`). `--list` shows the changes:

```console
$ pyxbuild inspect myapp-update-2.0.exe --list '*/mypkg/*'
M 4029  runtime/Lib/site-packages/mypkg/main.py  (carried)
A    4  runtime/Lib/site-packages/mypkg/data/new.yaml  (carried)
D       runtime/Lib/site-packages/mypkg/data/old.yaml
3 change(s); 1654 unchanged file(s) not listed
```

`A` added, `M` replaced, `D` removed; patches made by older versions do not
record which written files are new or which are removed and mark them all `W`.
`--extract` on a patch writes its payload as it travels: `patch.json`,
`blobs/<sha256>` and `_pyxbuild_patch.py`.

### Why not search for the magic

The onefile archive starts with `PYXARC01`, but so does a string in the
launcher's own code, so the first occurrence in the file is not the archive.
`inspect` (and the launcher) take the archive's offset and size from the
manifest at the end of the file (see [Design: Onefile](DESIGN.md#onefile-format-1));
`pyxbuild.inspector.open_contents()` does the same for your own scripts.

## `[tool.pyxbuild] require`

```toml
[tool.pyxbuild]
require = ["mypkg/data/*.yaml", "myapp/templates/**"]
```

Each pattern must match at least one file of the finished application, after
`exclude`, `slim` and `strip-sources` have done their work and before the
onefile archive is written. Otherwise the build fails and names every empty
pattern, with the reason when pyxbuild can tell it:

```console
$ pyxbuild build
...
error: required files are missing from the application ([tool.pyxbuild] require):
  'mypkg/data/*.yaml' matches no file: expected from mypkg-1.0-py3-none-any.whl, which installs mypkg/ but has no such file
  'myapp/templates/**' matches no file: myapp-2.1-py3-none-any.whl has 'myapp/templates/base.html', left out by exclude/slim pattern 'templates/'
  hint: patterns are relative to site-packages; a file the wheel lacks must be added to the package's data (and to the source tree it is built from)
```

* "expected from X, which installs ...": the wheel does not contain the file.
  The package's build configuration does not include it (package data), or the
  wheel was built from an incomplete copy of the sources; a wheel of a local
  package (`[tool.uv.sources]`) is built from the folder as it is.
* "X has ..., left out by exclude/slim pattern ...": the wheel has it, your
  `exclude` or a `slim` preset dropped it.
* "no wheel of the application has such a file": check the pattern.

`pyxbuild doctor` reports the same lines as errors.

**Paths are relative to site-packages**, like `exclude`: write
`mypkg/data/*.yaml`, not `runtime/Lib/site-packages/mypkg/data/*.yaml`. The
same pattern then works for every target (Windows and Linux lay out the runtime
differently), and pyxbuild can match it against the wheels to say which one
should have had the file. Patterns are gitignore-style, as in `exclude`: `*`
stays inside one folder, `**` crosses folders, a trailing `/` matches a
folder's contents, and a pattern without `/` matches at any depth
(`"*.yaml"`: any `.yaml` anywhere in site-packages). Files copied with
`include` need no `require`: `include` already fails when a pattern matches
nothing.

## Files left out by `exclude` and `slim`

The build log names the files that `exclude` and `slim` dropped from the
project's own wheel and from local packages (`[tool.uv.sources]`), by
comparing each wheel's file list (its `RECORD`) with what was installed, not
only their number:

```console
[win_amd64] excluded 3 file(s)
[win_amd64]   mypkg-1.0-py3-none-any.whl: mypkg/data/defaults.yaml, mypkg/data/schema.yaml
```

Files dropped from dependencies installed from an index are only counted:
removing their tests and stubs is what the patterns are for.

## Compared with PyInstaller

PyInstaller has `pyi-archive_viewer`, which lists and extracts the contents
of a onefile executable (its CArchive and the PYZ of compiled modules);
`pyxbuild inspect` is the counterpart for pyxbuild's archive, application
folders and patches. PyInstaller has no setting that fails the build when an
expected data file is missing; data files are collected by hooks
(`collect_data_files`), and a missing one shows up at run time. `require`
moves that failure to the build.
