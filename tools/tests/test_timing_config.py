"""The actual IHP STA corner must select its matching SRAM timing view."""

import fnmatch
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("corner, library", [
    ("nom_typ_1p20V_25C", "RM_IHPSG13_1P_1024x16_c2_bm_bist_typ_1p20V_25C.lib"),
    ("nom_fast_1p32V_m40C", "RM_IHPSG13_1P_1024x16_c2_bm_bist_fast_1p32V_m55C.lib"),
    ("nom_slow_1p08V_125C", "RM_IHPSG13_1P_1024x16_c2_bm_bist_slow_1p08V_125C.lib"),
])
def test_ihp_corner_selects_matching_sram_library(corner, library):
    # LibreLane Toolbox.filter_views matches corner names with fnmatch.
    # IHP names all three corners nom_*, unlike sky130's nom/min/max names.
    # The supplied SRAM fast view is characterized at -55 C, versus the
    # standard-cell fast view at -40 C; use the supplied fast SRAM view.
    config = json.loads((Path(__file__).resolve().parents[2] / "src/config.json").read_text())
    macro = config["MACROS"]["RM_IHPSG13_1P_1024x16_c2_bm_bist"]
    selected = [Path(p).name for pattern, paths in macro["lib"].items()
                if fnmatch.fnmatchcase(corner, pattern) for p in paths]
    assert selected == [library], (corner, selected)
