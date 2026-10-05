"""The declared chip clock, read from info.yaml (the one place it is set)."""

import os
import re

_INFO = os.path.join(os.path.dirname(__file__), "..", "..", "info.yaml")
with open(_INFO) as _f:
    CLOCK_HZ = int(re.search(r"^\s*clock_hz:\s*(\d+)", _f.read(), re.M).group(1))
# Simulation period in ps, even so both clock phases are whole picoseconds.
CLOCK_PS = 2 * round(0.5e12 / CLOCK_HZ)
