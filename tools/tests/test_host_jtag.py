"""Host library JTAG use case against the chip model (no RTL)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tempo.harness import Harness  # noqa: E402
from tempo.models.jtag import JtagChain, Tap  # noqa: E402
from tempo.transport import model_transport  # noqa: E402

from tempo_host.link import Link  # noqa: E402
from tempo_host import jtag as J  # noqa: E402

STM32 = 0x4BA00477  # Arm JTAG-DP
FPGA = 0x0362D093   # Xilinx part


def board(pins=("uio0", "uio1", "uio2", "uio3"), taps=None):
    h = Harness(None, boot="host", pullups={"uio2"})
    chain = h.attach(JtagChain(*pins, taps or [Tap(4, STM32), Tap(5, None), Tap(6, FPGA)]))
    return h, chain, Link(model_transport(h))


def test_idcode_chain_and_length():
    h, chain, link = board()
    j = J.Jtag(link, tck=0, tms=1, tdi=2, tdo=3, period=10)
    ids = j.idcodes()
    # nearest TDO first: the FPGA, the TAP without IDCODE, then the Arm DP
    assert ids == [FPGA, None, STM32], ids
    assert "Xilinx" in j.describe(FPGA) and "Arm" in j.describe(STM32)
    assert j.chain_length() == 3
    assert j.ir_length() == 4 + 5 + 6
    link.check()


def test_pin_finder_on_scrambled_header():
    # TCK=uio5, TMS=uio2, TDI=uio7, TDO=uio4
    h, chain, link = board(pins=("uio5", "uio2", "uio7", "uio4"), taps=[Tap(4, STM32)])
    found = J.find_pins(link, candidates=(2, 4, 5, 7), period=10)
    assert found == {"tck": 5, "tms": 2, "tdi": 7, "tdo": 4, "idcode": STM32}, found
    link.check()
