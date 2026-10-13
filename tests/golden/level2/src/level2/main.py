"""Golden set level 2 (see docs/TESTING.md)."""

import io
import os

from ._smoke import run_checks


def check_matplotlib():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    ax.plot([1, 2, 3], [3, 1, 2])
    ax.set_title("fonts and package data")
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    assert buf.getvalue()[:8] == b"\x89PNG\r\n\x1a\n"


def check_scipy():
    from scipy.optimize import minimize

    res = minimize(lambda x: (x[0] - 3) ** 2 + (x[1] + 1) ** 2, [0.0, 0.0])
    assert abs(res.x[0] - 3) < 1e-4 and abs(res.x[1] + 1) < 1e-4


def check_pywin32():
    import pythoncom
    import pywintypes
    import win32api

    assert win32api.GetCurrentProcessId() == os.getpid()
    assert pywintypes.TRUE == 1
    pythoncom.CoInitialize()
    pythoncom.CoUninitialize()


def check_psutil():
    import psutil

    assert os.getpid() in psutil.pids()
    assert psutil.Process().name().lower().startswith("python")


def check_orjson():
    import orjson

    assert orjson.loads(orjson.dumps({"a": [1, 2.5, None]})) == {"a": [1, 2.5, None]}


def check_pyzmq():
    import zmq

    ctx = zmq.Context()
    rep, req = ctx.socket(zmq.REP), ctx.socket(zmq.REQ)
    rep.bind("inproc://golden")
    req.connect("inproc://golden")
    req.send(b"ping")
    assert rep.recv() == b"ping"
    rep.send(b"pong")
    assert req.recv() == b"pong"
    ctx.destroy(linger=0)


def check_opencv():
    import cv2
    import numpy as np

    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    frame[:, :32] = (0, 0, 255)
    ok, jpg = cv2.imencode(".jpg", frame)
    assert ok and cv2.imdecode(jpg, cv2.IMREAD_COLOR).shape == (48, 64, 3)


def check_pyside6():
    os.environ.setdefault("QT_QPA_PLATFORM", os.environ.get("PYXBUILD_TEST_QPA", "windows"))
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QLabel

    app = QApplication([])
    label = QLabel("golden")
    label.show()
    QTimer.singleShot(200, app.quit)
    assert app.exec() == 0
    assert app.platformName() in ("windows", "offscreen", "minimal")


def run():
    return run_checks([check_matplotlib, check_scipy, check_pywin32, check_psutil,
                       check_orjson, check_pyzmq, check_opencv, check_pyside6])
