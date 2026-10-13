"""Golden set level 1 (see docs/TESTING.md): one check per class of problem."""

import io
import os

from ._smoke import run_checks


def check_numpy():
    import numpy as np

    m = np.array([[4.0, 7.0, 2.0], [3.0, 6.0, 1.0], [2.0, 5.0, 3.0]])
    assert np.allclose(m @ np.linalg.inv(m), np.eye(3))


def check_pandas():
    import pandas as pd

    df = pd.read_csv(io.StringIO("k,v\na,1\nb,2\na,3\n"))
    assert df.groupby("k")["v"].sum().to_dict() == {"a": 4, "b": 2}
    assert pd.Timestamp("2026-01-01", tz="Europe/Berlin").tzinfo is not None  # tzdata


def check_pillow():
    from PIL import Image

    img = Image.new("RGB", (32, 16), (200, 30, 30))
    for fmt in ("PNG", "JPEG", "WEBP"):
        buf = io.BytesIO()
        img.save(buf, fmt)
        buf.seek(0)
        assert Image.open(buf).size == (32, 16)


def check_requests_https():
    if os.environ.get("PYXBUILD_TEST_OFFLINE"):
        import certifi

        assert os.path.exists(certifi.where())
        return
    import requests

    assert requests.get("https://pypi.org/simple/", timeout=30).status_code == 200


def check_lxml():
    from lxml import etree

    root = etree.fromstring(b"<a><b n='1'/><b n='2'/></a>")
    assert root.xpath("count(//b)") == 2.0


def check_cryptography():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, b"secret", None)
    assert AESGCM(key).decrypt(nonce, ct, None) == b"secret"


def check_pydantic():
    from pydantic import BaseModel, ValidationError

    class M(BaseModel):
        x: int

    assert M(x="3").x == 3
    try:
        M(x="nope")
    except ValidationError:
        pass
    else:
        raise AssertionError("validation did not fail")


def check_tkinter():
    import tkinter

    assert tkinter.Tcl().eval("info patchlevel")
    if os.environ.get("PYXBUILD_TEST_NO_DISPLAY"):
        return
    root = tkinter.Tk()
    root.after(50, root.destroy)
    root.mainloop()


def run():
    return run_checks([check_numpy, check_pandas, check_pillow, check_requests_https,
                       check_lxml, check_cryptography, check_pydantic, check_tkinter])
