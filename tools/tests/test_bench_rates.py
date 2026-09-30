"""A rate benchmark must reject bad inputs and damaged retained data."""

from pathlib import Path
import subprocess
import sys

import pytest

import bench_rates
from bench_rates import validate_uart_words
from tempo_host.uart import LOST

ROOT = Path(__file__).resolve().parents[2]


def test_uart_validation_accepts_only_marked_missing_data():
    expected = bytes(range(16))
    words = list(expected[:4]) + [LOST | 6] + list(expected[7:])
    data, errors = validate_uart_words(expected, words)
    assert data == expected[:4] + expected[6:]
    assert errors == {"framing": 0, "parity": 0, "loss_markers": 1}


@pytest.mark.parametrize("words", [
    [0, 1, 2, 3, LOST | 6, 7, 99, 9],  # corrupt byte after a valid loss marker
    [0, 2, 1, 3, LOST | 6, 7, 8, 9],   # unmarked reordering
    [99, 1, 2, 3, LOST | 6, 7, 8, 9],  # corruption before the marker
    [0, 1, 2, 3, 6, 7, 8, 9],         # loss without a marker
])
def test_uart_validation_rejects_corruption_even_when_loss_is_marked(words):
    with pytest.raises(AssertionError):
        validate_uart_words(bytes(range(16)), words)


@pytest.mark.parametrize("size", [1, 513, 1023, 4097])
def test_unsupported_bulk_size_fails_before_any_simulation(size):
    result = subprocess.run([sys.executable, str(ROOT / "tools/bench_rates.py"),
                             "--spi-bytes", str(size)], timeout=2,
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "multiple of 512" in result.stderr


@pytest.mark.parametrize("size", [512, 4096])
def test_cli_accepts_supported_boundary_sizes(size, monkeypatch):
    calls = []

    def spi(*args):
        calls.append(("spi", args[3], args[4]))
        return {}

    def uart(*args):
        calls.append(("uart", args[3]))
        return {}

    # Exercise argument parsing and dispatch without running three large
    # device simulations merely to validate a supported input boundary.
    monkeypatch.setattr(bench_rates, "spi_read", spi)
    monkeypatch.setattr(bench_rates, "uart_receive", uart)
    monkeypatch.setattr(bench_rates, "manifest", lambda: {})
    monkeypatch.setattr(bench_rates.subprocess, "check_output", lambda *a, **k: b"fixture\n")
    monkeypatch.setattr(sys, "argv", ["bench_rates.py", "--spi-bytes", str(size), "--costs", "0"])
    bench_rates.main()
    assert calls == [("spi", size, False), ("spi", size, True), ("uart", 1024)]
