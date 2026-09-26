"""rp2350.py and serve.py under the MicroPython unix port, with stub machine
and ttboard modules: checks the MicroPython API surface they use."""

import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def test_board_transport_under_micropython():
    mpy = shutil.which("micropython")
    if mpy is None:
        pytest.skip("micropython unix port not installed")
    env = dict(os.environ, MICROPYPATH=os.pathsep.join(
        [os.path.join(HERE, "mpy_stubs"), os.path.join(HERE, "..", "..", "host"), ".frozen"]))
    r = subprocess.run([mpy, os.path.join(HERE, "mpy_board_check.py")], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "BOARD CHECK OK" in r.stdout, r.stdout + r.stderr
