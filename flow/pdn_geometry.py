# SPDX-License-Identifier: Apache-2.0
"""Pure geometry helpers for SRAM power straps (coordinates share one unit)."""

import re


def cut_intervals(start, end, blocked, margin=0):
    """Subtract obstruction intervals and their clearance from a strap."""
    out = []
    cursor = start
    for low, high in sorted(blocked):
        low, high = low - margin, high + margin
        if high <= cursor or low >= end:
            continue
        if low > cursor:
            out.append((cursor, low))
        cursor = max(cursor, high)
        if cursor >= end:
            break
    if cursor < end:
        out.append((cursor, end))
    return out


def metal4_obstructions(text):
    """Return the macro's Metal4 OBS rectangles and explicit LEF spacing."""
    obs = text.split("  OBS", 1)[-1]
    match = re.search(r"LAYER Metal4 SPACING\s+(\S+)\s*;(.*?)(?=\n\s*(?:LAYER|END))", obs, re.S)
    if match is None:
        raise ValueError("no Metal4 OBS block with explicit spacing")
    rects = [tuple(map(float, r)) for r in re.findall(
        r"RECT\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*;", match.group(2))]
    return rects, float(match.group(1))
