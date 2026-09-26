"""PDN geometry must leave the SRAM's obstructed power-domain gap open."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "flow"))

from pdn_geometry import cut_intervals, metal4_obstructions


def test_sram_power_gap_is_cut_with_clearance():
    # Real Magic feedback coordinates (nm), SRAM origin y=81 um. The
    # obstruction spans local y=39.085..45.205; the power pins stop/start
    # 260 nm outside it.
    assert cut_intervals(0, 600000, [(120085, 126205)], 260) == [
        (0, 119825), (126465, 600000)]


def test_overlapping_blocks_merge_without_short_segments():
    assert cut_intervals(0, 100, [(30, 60), (50, 80), (20, 35)], 2) == [(0, 18), (82, 100)]


def test_full_block_and_outside_blocks():
    assert cut_intervals(10, 20, [(0, 30)], 0) == []
    assert cut_intervals(10, 20, [(-10, 0), (30, 40)], 1) == [(10, 20)]
    assert cut_intervals(10, 20, [], 0) == [(10, 20)]


def test_parse_only_metal4_obstructions():
    text = """
  PIN VDD!
    LAYER Metal4 ;
    RECT 4.26 0 7.07 38.825 ;
  END VDD!
  OBS
    LAYER Metal3 ;
      RECT 0 0 100 100 ;
    LAYER Metal4 SPACING 0.21 ;
      RECT 0 39.085 9.62 45.205 ;
      RECT 0 0 4 336.46 ;
  END
"""
    assert metal4_obstructions(text) == ([(0, 39.085, 9.62, 45.205), (0, 0, 4, 336.46)], 0.21)
