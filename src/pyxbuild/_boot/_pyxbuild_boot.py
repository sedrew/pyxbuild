"""Start-up shim of applications built by pyxbuild (copied to runtime/Lib).

The launcher runs ``python -I -c "import _pyxbuild_boot;_pyxbuild_boot.main('<script>')"``.
This module reads ``runtime/pyxbuild.json`` and prepares the process before
calling the entry point:

* removes environment variables that would point the app at another
  installation (``clean-env``, e.g. TCL_LIBRARY, QT_PLUGIN_PATH);
* sets variables from ``[tool.pyxbuild.env]`` (``{app_dir}``, ``{data_dir}``
  and ``{localappdata}`` are expanded);
* for GUI scripts: gives ``print``/``logging`` a log file instead of
  ``sys.stdout = None``, shows unhandled exceptions in a message box instead
  of exiting silently, sets the AppUserModelID and per-monitor DPI awareness;
* optionally lets ``ssl`` use the Windows certificate store (``truststore``).

Only the standard library is used; failures here never prevent the app from
starting.
"""

import importlib
import json
import os
import sys

_LOG_LIMIT = 1 << 20


def _config():
    with open(os.path.join(sys.prefix, "pyxbuild.json"), encoding="utf-8") as f:
        return json.load(f)


def _expand(value, names):
    for key, path in names.items():
        value = value.replace("{" + key + "}", path)
    return value


def _gui_streams(log_path):
    """pythonw.exe has no console: sys.stdout/stderr are None and print() fails."""
    if sys.stdout is not None and sys.stderr is not None:
        return None
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        if os.path.exists(log_path) and os.path.getsize(log_path) > _LOG_LIMIT:
            os.replace(log_path, log_path + ".1")
        stream = open(log_path, "a", encoding="utf-8", buffering=1,  # noqa: SIM115
                      errors="backslashreplace")
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    return log_path


def _message_box(title, text):
    if os.environ.get("PYXBUILD_BOOT_NO_DIALOG"):
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # MB_ICONERROR
    except Exception:
        pass


def _gui_excepthook(app_name, log_path):
    import traceback

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            sys.stderr.write(text)
            sys.stderr.flush()
        except Exception:
            pass
        lines = text.strip().splitlines()[-12:]
        where = f"\n\nDetails were written to:\n{log_path}" if log_path else ""
        _message_box(f"{app_name} error", "\n".join(lines) + where)

    sys.excepthook = hook


def _gui_process(app_id, dpi_aware):
    try:
        import ctypes

        if app_id:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        if dpi_aware:
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2; pythonw.exe declares
            # none, so the UI would be bitmap-scaled (blurry) on high-DPI screens
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass


def main(script):
    cfg = _config()
    launcher = cfg["launchers"][script]
    boot = cfg.get("boot", {})
    # the launcher passes its own path first; it becomes sys.argv[0]
    sys.argv[0:2] = sys.argv[1:2]
    app_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    app_name = cfg.get("project", {}).get("name", script)
    localappdata = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    names = {"app_dir": app_dir, "localappdata": localappdata,
             "data_dir": os.path.join(localappdata, app_name)}

    for var in boot.get("clean-env", []):
        os.environ.pop(var, None)
    for key, value in boot.get("env", {}).items():
        os.environ[key] = _expand(value, names)

    if launcher.get("gui"):
        log_path = None
        if boot.get("gui-log", True):
            log_path = _gui_streams(os.path.join(names["data_dir"], "logs", script + ".log"))
        _gui_excepthook(app_name, log_path)
        _gui_process(boot.get("app-id"), boot.get("dpi-aware", True))

    if boot.get("truststore"):
        try:
            import truststore

            truststore.inject_into_ssl()
        except ImportError:
            pass

    module, _, attr = launcher["entry-point"].partition(":")
    target = importlib.import_module(module)
    for part in attr.split("."):
        target = getattr(target, part)
    sys.exit(target())
