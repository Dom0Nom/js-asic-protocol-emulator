"""JTAG TAP chain model (IEEE 1149.1 behaviour; TI SSYA002C, OpenOCD).

TMS and TDI are sampled on the rising edge of TCK; TDO changes on the
falling edge and is driven only in Shift-DR and Shift-IR (high impedance
otherwise). On Test-Logic-Reset the instruction register selects IDCODE
where a TAP has one, else BYPASS; the IR captures ...01; BYPASS captures 0.
TDI enters the first TAP in `taps`, TDO leaves the last one.
"""

NEXT = {  # state: (TMS=0, TMS=1)
    "TLR": ("RTI", "TLR"), "RTI": ("RTI", "SELDR"),
    "SELDR": ("CAPDR", "SELIR"), "CAPDR": ("SHIFTDR", "EXIT1DR"),
    "SHIFTDR": ("SHIFTDR", "EXIT1DR"), "EXIT1DR": ("PAUSEDR", "UPDDR"),
    "PAUSEDR": ("PAUSEDR", "EXIT2DR"), "EXIT2DR": ("SHIFTDR", "UPDDR"),
    "UPDDR": ("RTI", "SELDR"), "SELIR": ("CAPIR", "TLR"),
    "CAPIR": ("SHIFTIR", "EXIT1IR"), "SHIFTIR": ("SHIFTIR", "EXIT1IR"),
    "EXIT1IR": ("PAUSEIR", "UPDIR"), "PAUSEIR": ("PAUSEIR", "EXIT2IR"),
    "EXIT2IR": ("SHIFTIR", "UPDIR"), "UPDIR": ("RTI", "SELDR"),
}


class Tap:
    def __init__(self, ir_len=4, idcode=None, idcode_ir=None):
        self.ir_len = ir_len
        self.idcode = idcode
        self.bypass_ir = (1 << ir_len) - 1
        self.idcode_ir = idcode_ir if idcode_ir is not None else (1 if idcode is not None else None)
        self.ir = 0
        self.reg = []  # the selected shift register, LSB (nearest TDO) first
        self.reset()

    def reset(self):
        self.ir = self.idcode_ir if self.idcode is not None else self.bypass_ir

    def capture_dr(self):
        if self.idcode is not None and self.ir == self.idcode_ir:
            self.reg = [(self.idcode >> i) & 1 for i in range(32)]
        else:
            self.reg = [0]  # BYPASS (and any other instruction here) captures 0

    def capture_ir(self):
        self.reg = [1, 0] + [0] * (self.ir_len - 2)  # ...01

    def update_ir(self):
        self.ir = sum(b << i for i, b in enumerate(self.reg[:self.ir_len]))


class JtagChain:
    def __init__(self, tck, tms, tdi, tdo, taps):
        self.pins = (tck, tms, tdi, tdo)
        self.taps = list(taps)
        self.state = "TLR"
        self._prev = 0
        self._tdo = None
        for t in self.taps:
            t.reset()

    def step(self, cycle, nets):
        tck, tms, tdi, tdo = self.pins
        clk = nets.get(tck, 0)
        if clk and not self._prev:
            self._rise(nets.get(tms, 1), nets.get(tdi, 1))
        elif self._prev and not clk:
            if self.state in ("SHIFTDR", "SHIFTIR"):
                self._tdo = self.taps[-1].reg[0]
            else:
                self._tdo = None
        self._prev = clk
        return {tdo: self._tdo}

    def _rise(self, tms, tdi):
        s = self.state
        if s == "CAPDR":
            for t in self.taps:
                t.capture_dr()
        elif s == "CAPIR":
            for t in self.taps:
                t.capture_ir()
        elif s in ("SHIFTDR", "SHIFTIR"):
            b = tdi
            for t in self.taps:
                out = t.reg[0]
                t.reg = t.reg[1:] + [b]
                b = out
        self.state = NEXT[s][tms]
        if self.state == "TLR":
            for t in self.taps:
                t.reset()
        elif self.state == "UPDIR":
            for t in self.taps:
                t.update_ir()
