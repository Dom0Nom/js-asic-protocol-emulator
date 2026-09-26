"""The CLI against the chip model, and the same sessions replayed under the
MicroPython unix port (host library compatibility)."""

import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tempo import sessions  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
EXPECT = {
    "i2c": ["found: 0x50 0x68", "000010  10 11 12 13", "000000  ff ff ff ff"],
    "flash": ["JEDEC ID ef 40 18", "SFDP rev 1.", "000100  "],
    "jtag": ["3 device(s)", "Xilinx", "no IDCODE (BYPASS)", "Arm"],
    "swd": ["DPIDR 0x2ba01477: Arm", "ROM table at 0xe00ff000", "component"],
    "uart": ["nearest standard rate 115200"],
    "la": ["50000.0 Hz, duty 0.300"],
    "trigger": ["3 pulse(s) generated"],
}


@pytest.mark.parametrize("name", sorted(sessions.SCENARIOS))
def test_cli_and_micropython_replay(name, tmp_path):
    path = str(tmp_path / (name + ".json"))
    out = sessions.save(name, path)
    text = "\n".join(out)
    assert "error:" not in text, text
    for frag in EXPECT[name]:
        assert frag in text, (frag, text)
    # control: the replayer itself under CPython
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "mpy_replay.py"), path],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "REPLAY OK" in r.stdout, r.stdout + r.stderr
    mpy = shutil.which("micropython")
    if mpy is None:
        pytest.skip("micropython unix port not installed")
    r = subprocess.run([mpy, os.path.join(ROOT, "tools", "mpy_replay.py"), path],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "REPLAY OK" in r.stdout, r.stdout + r.stderr
