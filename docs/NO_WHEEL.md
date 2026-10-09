# When a dependency has no wheel for the target

pyxbuild installs wheels and never runs a package's build on behalf of the
target by default. When a dependency has no `win_amd64` / `abi3` /
`py3-none-any` wheel, these are the options, safest first. `pyxbuild
doctor` reports which one applies.

| # | Strategy | How | Works for | Risk |
|---|---|---|---|---|
| 1 | Older version with a wheel | automatic: uv with `--only-binary :all:` backtracks | most cases | silent downgrade — `doctor` now reports packages *held back* because the newest release has no wheel |
| 2 | Older Python | `python = "3.12"` / `--python` | new CPython without wheels yet | none |
| 3 | Pure-Python wheel from the index | automatic (aiohttp, yarl, frozenlist, propcache, simplejson, wrapt, charset-normalizer publish one) | those packages | slower |
| 4 | Pure-Python build from sdist | `pure-python = ["msgpack", "pyyaml"]` | packages with an official "no extension" switch (table below) | slower; only `py3-none-any` results are accepted |
| 5 | Wheel built on Windows | build in CI (cibuildwheel / `pip wheel` on `windows-latest`), add with `find-links = ["wheels/"]` | everything | needs a Windows CI job |
| 6 | Cross-compile on the host (experimental) | `cross-compile = ["markupsafe"]` (needs `pip install pyxbuild[cross]`) | self-contained C/C++/Cython setuptools extensions | see below |
| 7 | Pure-Python sdist | `allow-sdist = true` | sdists without compiled code | none (wheel must be `py3-none-any`) |

Never done automatically: downloading MSVC / Windows SDK files (xwin,
msvc-wine — Microsoft's licence forbids redistribution), third-party binary
wheels, conda packages.

## Pure-Python switches (`fallbacks.py`)

| Package | Switch | Pure wheel on PyPI already |
|---|---|---|
| msgpack | `MSGPACK_PUREPYTHON=1` | no |
| PyYAML | `PYYAML_FORCE_LIBYAML=0` | no |
| multidict | `MULTIDICT_NO_EXTENSIONS=1` | no |
| aiohttp | `AIOHTTP_NO_EXTENSIONS=1` | yes |
| frozenlist / yarl / propcache | `*_NO_EXTENSIONS=1` | yes |
| simplejson | `DISABLE_SPEEDUPS=1` | yes |
| wrapt | `WRAPT_INSTALL_EXTENSIONS=false` | yes |
| charset-normalizer | pure by default | yes |

markupsafe has no switch (it falls back only when compiling *fails*); use
cross-compilation instead.

## Cross-compilation with zig (experimental)

The sdist's setup.py, Cython and packaging run on the host with a CPython of
the same X.Y (uv provides it). Compiling and linking go to
`zig cc -target x86_64-windows-gnu` against the *target* runtime's
`include/` and `libs/python3XY.lib`. zig links the UCRT — the C runtime
MSVC-built CPython uses — so the `.pyd` imports only `python3XY.dll`,
`KERNEL32` and `api-ms-win-crt-*`. The wheel is re-tagged `win_amd64` and
refused if it contains any host (ELF/Mach-O) binary.

Details handled: `MS_WIN64` for Python ≤ 3.11 (its `pyconfig.h` sets it for
MinGW only since 3.12); `#include <Windows.h>` and other SDK spellings on a
case-sensitive file system (shim headers); projects that subclass distutils'
or Cython's `build_ext` (patched lazily when the build imports them, because
importing setuptools early breaks some setup.py files); `SETUPTOOLS_EXT_SUFFIX`
is *not* set (zstandard switches into its own broken cross path with it);
linker side outputs (`.lib`, `.pdb`) removed.

Results on real PyPI sdists, built on Linux and run under Wine
(`tools/cross_matrix.py`, Python 3.11 and 3.12 identical):

| Package | Kind | Result |
|---|---|---|
| markupsafe, simplejson, bitarray, regex, wrapt | C | ✅ |
| lz4, zstandard | C + bundled C library | ✅ |
| msgpack | Cython + C++ | ✅ |
| ujson | C + C++ (double-conversion) | ✅ |
| frozenlist, propcache, yarl, cytoolz | Cython | ✅ |
| PyYAML | probes libyaml on the host | ❌ → use `pure-python` |
| psutil | chooses sources by `sys.platform` | ❌ expected |
| cffi | needs libffi for Windows | ❌ expected |
| Levenshtein (scikit-build-core), numpy (meson-python) | not setuptools | ❌ refused: host binary detected |
| multidict 7.1.0 | no sdist published | — |

14 of 19 pass; every failure is reported with a reason, none produces a
broken wheel. Limits: only the C ABI is safe across toolchains (zig links
libc++ statically; C++ objects must not cross into MSVC-built libraries);
setup.py logic that inspects the host (`sys.platform`, compiler probes,
`pkg-config`) sees Linux; Fortran is not supported. Run results on real
Windows before relying on a cross-compiled extension.

Rust/PyO3 extensions can be cross-built with `maturin --target
x86_64-pc-windows-msvc` (cargo-xwin) outside pyxbuild — this downloads the
MSVC CRT and requires accepting Microsoft's licence — and added through
`find-links`.
