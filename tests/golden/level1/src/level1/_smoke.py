"""Shared smoke-test runner, copied into each golden app as <app>/_smoke.py."""

import sys
import traceback


def run_checks(checks):
    only = set(sys.argv[1:])
    failed = 0
    for check in checks:
        name = check.__name__.removeprefix("check_")
        if only and name not in only:
            continue
        try:
            check()
            print(f"OK {name}", flush=True)
        except Exception:
            failed += 1
            print(f"FAIL {name}: {traceback.format_exc()}", flush=True)
    return 1 if failed else 0
