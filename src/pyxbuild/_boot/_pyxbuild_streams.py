"""Standard streams for windowless processes of applications built by pyxbuild.

pythonw.exe starts with ``sys.stdout``/``sys.stderr`` set to None: ``print()``
is silently dropped, but ``sys.stdout.write()``, ``logging`` handlers created
early or libraries that flush the streams raise AttributeError. That also
hits every child process (multiprocessing, ProcessPoolExecutor), which never
runs ``_pyxbuild_boot``. ``_pyxbuild_streams.pth`` in site-packages imports
this module at the start of every interpreter of the bundle; it does nothing
when the streams exist. Output goes to the GUI log the main process chose
(``PYXBUILD_GUI_LOG``, inherited), otherwise nowhere.
"""

import os
import sys

PLACEHOLDER = None


def open_append(path):
    """A line-buffered text stream that several processes can append to at once.

    The C runtime's "a" mode seeks to the end before every write, so on
    Windows concurrent writers overwrite each other's lines. A handle opened
    with FILE_APPEND_DATA but without FILE_WRITE_DATA makes the system append
    every write atomically at the end of the file.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            import msvcrt
            from ctypes import wintypes

            create = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
            create.restype = wintypes.HANDLE
            create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                               wintypes.HANDLE]
            # FILE_APPEND_DATA; share read/write/delete; OPEN_ALWAYS; normal file
            handle = create(path, 0x0004, 0x0007, None, 4, 0x80, None)
            if handle and handle != wintypes.HANDLE(-1).value:
                fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
                stream = open(fd, "w", encoding="utf-8", buffering=1,  # noqa: SIM115
                              errors="backslashreplace")
                stream.buffer.raw.name = path  # not the descriptor number
                return stream
        except Exception:
            pass
    return open(path, "a", encoding="utf-8", buffering=1,  # noqa: SIM115
                errors="backslashreplace")


if sys.stdout is None or sys.stderr is None:
    try:
        _log = os.environ.get("PYXBUILD_GUI_LOG")
        if _log:
            PLACEHOLDER = open_append(_log)
    except OSError:
        PLACEHOLDER = None
    if PLACEHOLDER is None:
        PLACEHOLDER = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = PLACEHOLDER
    if sys.stderr is None:
        sys.stderr = PLACEHOLDER
