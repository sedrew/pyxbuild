# Open questions

Decisions made without asking while building the MVP autonomously. Each has
a default in the code; all of them are cheap to change now and expensive
later.

## Product behaviour

1. **Default Python version.** Without `python = "..."` pyxbuild bundles the
   *newest* supported version allowed by `requires-python` (3.14 today).
   Newest-minus-one would hit fewer "no wheel for cp314 yet" failures.
   (`config.py: ProjectConfig.choose_python`)
2. **Output folder name.** `dist/<name>-win_amd64/` (the plan said
   `dist/app/`). The target suffix keeps future targets apart.
3. **Lock file name.** `pylock.win_amd64.toml` (PEP 751 allows
   `pylock.<name>.toml`); `lock = "pylock.toml"` overrides. One lock per target,
   because a lock records a single resolved environment.
4. **Bundle check is advisory.** Missing DLLs are reported but do not fail
   the build; `--strict` makes errors fatal. Strict-by-default failed every
   PySide6 and pywin32 build because of optional plugins (`qsqloci.dll` needs
   `OCI.dll`, `win32ui.pyd` needs `mfc140u.dll`).
5. **pip is removed** from the bundled runtime. Keep it (≈10 MB) to let users
   `pip install` into an installed app?
6. **`-I` isolated mode** for every app: `PYTHONPATH`, `PYTHONHOME`, user
   site-packages and the current directory are ignored — and so are
   `PYTHONUTF8`/`PYTHONIOENCODING`. `utf8-mode = true` exists for that; should
   UTF-8 mode be the default (Python 3.15 makes it the default anyway)?
7. **Launcher = child process** (`runtime\python.exe` started by the stub)
   instead of embedding `python3XX.dll` in the launcher process. Simpler and
   `multiprocessing`/`sys.executable` work, but there are two processes and
   Task Manager shows `python.exe`. Embedding is possible later.
8. **No bytecode at build time.** First start compiles `.py` files (slower;
   impossible in a read-only `Program Files`). Precompiling needs a host
   interpreter of the exact target version (uv can provide one) and
   `--invalidation-mode unchecked-hash` for reproducibility.
9. **`include/` and `libs/`** of the runtime are dropped (only needed for
   compiling extensions).

## Security / trust

10. **Runtime pin**: one python-build-standalone release (20260718) with sha256
    in `runtimes.json`; updating it is a manual `tools/update_runtimes.py`
    run + review. Should CI check weekly for new releases?
11. **The stubs are committed binaries.** CI rebuilds them on three OSes and
    requires identical bytes, but a release process (who builds, who signs
    the stub, publishing hashes) is not defined. The skeptic's conditions
    (budget for signing the stub, antivirus false-positive plan) are open.
12. **VC++ runtime DLLs**: PBS ships `vcruntime140.dll`/`vcruntime140_1.dll`;
    `msvcp140*.dll`, `vcruntime140_threads.dll`, `concrt140.dll`,
    `mfc140u.dll` are not shipped. The doctor reports them and suggests the
    `msvc-runtime` package from PyPI (works: it installs the DLLs next to
    `python.exe`; verified with torch). Should pyxbuild add it automatically
    when the doctor finds VC++ imports (needs a licence decision — it is
    Microsoft's redistributable, packaged by a third party), or stay opt-in?

## Testing

13. **No clean-Windows run.** GitHub's windows-latest has the VC++
    Redistributable and Python installed. A real "clean Windows 11 without
    VC++" smoke test needs a VM or a Windows container job.
14. **Wine is used locally** (Wine 11.10 from Kron4ek/Wine-Builds, downloaded
    by `tools/get_wine.sh`). Ubuntu's Wine 9.0 cannot run numpy. Is pulling a
    third-party Wine build into CI acceptable?
15. **Golden level 3** (torch, playwright) is covered only by the
    compatibility matrix and the doctor's known-issues list, not by a
    dedicated test.

## Added in the second session

16. **`bytecode` default.** Off today (bundle +27 %), but without it the
    first start of a numpy/pandas app takes ~5 s on Windows and the app
    needs a writable folder. Turn it on by default?
17. **`cross-compile` is experimental.** 14/19 real sdists built from Linux
    and pass under Wine; nothing has run on real Windows yet except through
    pyxbuild's own tests. Keep it opt-in per package?
18. **`clean-env` default list** removes `TCL_LIBRARY`, `TK_LIBRARY`,
    `QT_PLUGIN_PATH`, `QT_QPA_PLATFORM_PLUGIN_PATH`, `QML*_IMPORT_PATH` at
    start-up. `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` are kept (corporate
    proxies rely on them). Right balance?
19. **GUI apps get DPI awareness by default** (`dpi-aware = true`). Apps
    that draw with Tk at fixed pixel sizes may look small on 200 % screens.
20. **Default `slim`** drops tests, idlelib, ensurepip, turtledemo, lib2to3.
    Should `tk` also be dropped automatically when nothing imports tkinter?
21. **The process is still `python.exe`/`pythonw.exe`** (taskbar icon,
    Task Manager, firewall prompt). A per-app copy of `pythonw.exe` with our
    resources would fix it; worth the extra complexity?

## Not done (out of the MVP as approved)

- onefile mode, win_arm64/macOS/Linux targets, installers (MSI/NSIS);
- `doctor` without building (it resolves and installs into a temp folder);
- DLLs loaded with `LoadLibrary` at run time (CUDA, Qt plugins) are invisible
  to the static PE import check;
- discussion of the manifest format on discuss.python.org; checking whether
  contributing to PyApp/pynsist is an alternative;
- licence review of everything in the bundle (OpenSSL, Tcl/Tk, MSVC runtime).
