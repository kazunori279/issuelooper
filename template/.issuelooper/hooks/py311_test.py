#!/usr/bin/env python3
"""The hooks must run on Python 3.11 (Debian 12's python3), whatever runs the tests.
Compiles every hook with a python3.11 found on PATH or by uv; skips if there is none.
Run: python3 py311_test.py"""

import pathlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent


def find_py311():
    if shutil.which("python3.11"):
        return shutil.which("python3.11")
    if shutil.which("uv"):
        r = subprocess.run(["uv", "python", "find", "--no-project", "3.11"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


py = find_py311()
if not py:
    print("skip: no python3.11 (install one with `uv python install 3.11`)")
    sys.exit(0)
files = sorted(str(p) for p in HERE.glob("*.py"))
src = "import sys\nfor f in sys.argv[1:]:\n    compile(open(f, encoding='utf-8').read(), f, 'exec')\n"
r = subprocess.run([py, "-c", src, *files], capture_output=True, text=True)
if r.returncode != 0:
    print(r.stderr.strip())
    sys.exit(1)
print(f"all {len(files)} hooks compile on Python 3.11")
