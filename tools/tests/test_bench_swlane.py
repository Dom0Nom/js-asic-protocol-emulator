"""Key numbers of the software bit-bang vs lane UART benchmark (cycle model,
25 MHz). Full sweep and JSON: python tools/bench_swlane.py."""

import bench_swlane as b

ONE_M = 25.0
B115200 = b.CLOCK / 115200


def test_one_mbaud_cost_and_error():
    sw = b.run_single("sw_main", ONE_M, data=b.LONG)
    lane = b.run_single("lane_main", ONE_M, data=b.LONG)
    assert sw["ok"] and lane["ok"]
    # software: 49 instructions per byte (integer loop), spins in waitt between edges
    assert round(sw["instr_per_byte"]) == 49
    assert sw["occupied_fraction"] == 1.0
    # edges land only on the thread's slot (every 4 cycles): 3 cycles = 12 % of a bit
    assert sw["stream_max_abs"] == 3 and not sw["within_2pct"]
    # lane: 5 instructions per byte, exact edges at an integer period
    assert round(lane["instr_per_byte"]) == 5
    assert lane["busy_fraction"] < 0.09
    assert lane["stream_max_abs"] == 0 and lane["within_2pct"]


def test_load_on_other_threads_does_not_move_edges():
    for entry in ("sw_main", "lane_main"):
        quiet = b.run_single(entry, ONE_M, load=False)
        busy = b.run_single(entry, ONE_M, load=True)
        assert quiet["ok"] and busy["ok"]
        assert quiet["edges"] == busy["edges"], entry


def test_115200_edge_error():
    sw = b.run_single("sw_main", B115200, data=b.LONG)
    lane = b.run_single("lane_main", B115200, data=b.LONG)
    assert sw["ok"] and lane["ok"]
    assert 3 <= sw["stream_max_abs"] < 3.2
    assert lane["stream_max_abs"] < 1
    assert sw["within_2pct"] and lane["within_2pct"]


def test_max_sustainable_rate():
    # software: 5 slots per bit = 20 cycles = 1.25 Mbaud; one cycle less misses deadlines
    assert b.run_single("sw_main", 20)["ok"]
    assert b.run_single("sw_main", 20)["stream_max_abs"] == 0
    r = b.run_single("sw_main", 19)
    assert not r["ok"] and r["thread_late"]
    # lane: 5 instructions per 10-bit frame = 2 cycles per bit = 12.5 Mbaud (no pad model)
    assert b.run_single("lane_main", 2)["ok"]
    assert not b.run_single("lane_main", 1)["ok"]


def test_lane_leaves_slots_free_on_the_same_thread():
    r = b.run_single("lpoll_main", ONE_M, data=b.LONG)
    assert r["ok"] and r["stream_max_abs"] == 0
    assert r["instr_per_byte"] == b.LPOLL_BYTE_PATH
    assert r["free_fraction"] > 0.87


def test_second_uart_on_the_same_thread():
    sw = b.run_dual("swdual_main", 217, 250)
    lane = b.run_dual("ldual_main", 217, 250)
    assert all(c["decoded"] for c in sw["channels"] + lane["channels"])
    assert sw["thread_late"]
    assert [c["stream_max_abs"] for c in sw["channels"]] == [31, 56]
    assert [c["stream_max_abs"] for c in lane["channels"]] == [0, 0]
    assert not lane["thread_late"] and lane["work_slot_fraction"] > 0.6
    fast_sw = b.run_dual("swdual_main", 40, 48)
    fast_lane = b.run_dual("ldual_main", 40, 48)
    assert not any(c["decoded"] for c in fast_sw["channels"])
    assert all(c["within_2pct"] for c in fast_lane["channels"])


def test_shipped_uart_firmware_cost():
    assert b.uart_s_cost()["instr_per_char"] == 15.875


def test_manifest_covers_sources():
    m = b.manifest()
    assert "fw/bench_swuart.s" in m and "tools/tempo/sim.py" in m
    assert all(len(h) == 64 for h in m.values())
