# Go as builder or launcher — and what TypeScript did

## TypeScript 7 ("Corsa", typescript-go)

* **Why Go** ([FAQ](https://github.com/microsoft/typescript-go/discussions/411)):
  it was a *port*, not a rewrite, and Go's structure matched the existing
  code; control over memory layout; GC cost negligible for batch
  compilation; good ergonomics for walking polymorphic ASTs. The work is
  CPU-bound type checking over huge code bases — Go gave roughly 8–12×.
* **Distribution**: `typescript@7` declares ~20 `optionalDependencies`
  (`@typescript/typescript-win32-x64`, `-linux-arm64`, …) with `os`/`cpu`
  fields; npm installs only the matching one and `bin/tsc` is a tiny Node
  shim that executes the native binary (`tsc.exe` ≈ 24.5 MB). esbuild uses
  the same pattern (26 platform packages, ≈ 11.7 MB binary) plus a
  `postinstall` fallback when optional dependencies are skipped.
* **Cross-compilation**: Go builds every target from one host with
  `GOOS`/`GOARCH` and `CGO_ENABLED=0`.

The Python equivalent of that pattern is one `py3-none-<platform>` wheel
per platform — exactly how uv, ruff, cmake, ninja and ziglang are shipped.

## Measured for pyxbuild's launcher

| Launcher | Size | Imports |
|---|---|---|
| pyxbuild C stub, zig, no CRT (current) | **7 KB** | KERNEL32, USER32 |
| pyxbuild C stub, zig, mingw CRT (before) | 61 KB | UCRT, KERNEL32, USER32 |
| Rust std, opt-level z, LTO, panic=abort | ~378 KB | UCRT, ntdll |
| Go, `-trimpath -ldflags="-s -w"` | 1.6 MB | KERNEL32 |

Go-specific concerns: antivirus false positives on Go binaries are well
documented ([Go forum](https://forum.golangbridge.org/t/trojan-script-wacatac-b-ml-alert-on-windows-for-any-golang-build/35777),
[golang/go#30969](https://golang.org/issue/30969)); the Go runtime installs
its own console control handling (the launcher must let Ctrl+C reach the
child); size multiplies because pyxbuild writes one launcher per entry point
**and per dependency console script** (20 scripts: ≈ 32 MB in Go vs 140 KB
now).

## Conclusions

1. **The core stays Python.** TypeScript moved because type checking is
   CPU-bound; pyxbuild downloads, hashes, unpacks and writes metadata, and
   its CPU-heavy step (resolution) is already native (uv). Python gives the
   reference implementations of the standards (`packaging`, `installer`) —
   rewriting PEP 440/508/425 logic in Go would risk silent incorrectness.
2. **The launcher stays C.** 7 KB, reproducible, no runtime dependency,
   smallest antivirus surface. If memory safety is ever required, a
   `no_std` Rust stub (the approach of uv's trampoline) fits better than Go.
3. **The per-platform-package pattern applies to host tools, not stubs.**
   pyxbuild already gets its host binaries that way (uv, optionally zig).
   Stubs are *target* artifacts of a few KB and belong in the single
   `py3-none-any` pyxbuild wheel, keyed by target.
4. Keep pinning `ziglang==0.16.0` — 0.17.0 has no Windows wheel on PyPI.
