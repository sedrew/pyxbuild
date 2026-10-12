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

if sys.stdout is None or sys.stderr is None:
    try:
        _log = os.environ.get("PYXBUILD_GUI_LOG")
        if _log:
            PLACEHOLDER = open(_log, "a", encoding="utf-8", buffering=1,  # noqa: SIM115
                               errors="backslashreplace")
    except OSError:
        PLACEHOLDER = None
    if PLACEHOLDER is None:
        PLACEHOLDER = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = PLACEHOLDER
    if sys.stderr is None:
        sys.stderr = PLACEHOLDER
