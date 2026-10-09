import sys


def run() -> int:
    import requests

    print("argv", ascii(sys.argv[1:]))
    print("executable", sys.executable)
    print("requests", requests.__version__)
    if "--https" in sys.argv:
        r = requests.get("https://pypi.org/simple/", timeout=30)
        print("https", r.status_code)
    code = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--exit=")), "0")
    return int(code)
