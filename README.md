# Tempo protocol emulator

Tempo is a programmable protocol processor for the [Jane Street ASIC
competition](https://blog.janestreet.com/protocol-emulator-asic-competition/),
targeting IHP 130 nm CMOS5L in 6×4 Tiny Tapeout tiles.

Four threads share a 16-bit datapath and a 1024-word SRAM. Two full lanes and
one lite lane move pins on independent timelines. Firmware supplies words,
symbols and deadlines; lanes handle sampling, clock pulses and line coding.
This lets one instruction set implement UART, SPI, I2C, JTAG, SWD, passive
monitoring and edge capture. NRZI, stuffing and CRC are reusable settings;
A USB low-speed packet PHY passes in RTL lockstep at the declared 25 MHz; USB enumeration and pad-level behaviour are untested.

The verification compares every RTL pad and instruction commit with a Python
cycle model. Independent target models check the protocols on the wire.
Constrained-random programs and formal proofs cover the core's isolation and
mailbox bounds. See [verification](docs/verification.md) for evidence,
the [bugs the method caught](docs/verification.md#bugs-the-method-caught)
and remaining work.

The declared clock is 25 MHz (40 ns; the RP2350 demo board makes it as
150 MHz / 6). The previous routed design at 50 MHz missed slow-corner setup
by 9.19 ns, so 50 MHz is a future target, not a claim. The routed 25 MHz
build at `b4b9fe8` (27,570 standard cells, 55% utilisation, 6x4 tiles) meets
setup and hold at every corner (worst slow-corner setup slack +3.09 ns, hold
+0.083 ns fast) with zero DRC, LVS, antenna, power-grid and illegal-overlap
violations, and passes all nine Tiny Tapeout precheck checks. Its routed
netlist passes the UART, SPI, I2C and demo cases at gate level, and the
UART/SPI/I2C cases with SDF back-annotation at the slow and fast corners.
Open electrical items: 121 max-slew pins at the slow corner and 13 max-cap
and 13 max-fanout pins; every timing path through them keeps at least 1.42 ns
of slack in slow-corner STA (details in
[verification](docs/verification.md#physical-signoff)).

## What it does differently

Compared with RP2040 PIO and TI PRU, Tempo trades speed for one larger shared
program store and line coding in hardware; at 25 MHz it is slower than both.
One 130-word image, `fw/demo_multibus.s`, runs UART, Manchester, a host-driven
I2C controller with clock stretching and a compute thread at once, and the RTL
test checks their wire-time overlap
(`test_demo_resident_multibus_with_busy_host_and_memory` in
[test/test_usecases.py](test/test_usecases.py)). Manchester is lane settings,
not added RTL. Bench images: logic analyser, SPI and I2C monitors, JTAG chain
scan and pin finder, SWD attach, 24C02 I2C EEPROM target, triggered pulse.
Measured against PRU-style software timing on the same core, a lane UART costs
5 thread instructions per byte instead of 49-68, keeps edges within one cycle
instead of up to 3 (12% of a bit at 1 Mbaud), and lets one thread drive two
UARTs at unrelated rates with zero edge error where the software loop is off
by 31-56 cycles (`tools/bench_swlane.py`, cycle model, two-UART case confirmed
on RTL). Sizes and the comparison: [architecture](docs/architecture.md#measured-comparison-with-pio-and-pru);
wiring and commands: [use cases](docs/use-cases.md).

## Run the checks

Needs Icarus Verilog and Python 3.11 or newer. Set up the environment from
the repository root:

```sh
uv venv --python 3.11 .venv     # or: python3 -m venv .venv
source .venv/bin/activate
uv pip install -r test/requirements.txt   # or: pip install -r test/requirements.txt
```

Then:

```sh
cd tools
python -m pytest -q tests/
cd ../test
make -B
make -B COCOTB_TEST_MODULES=test_random RAND_SEEDS=100
```

The assembler and cycle model live in `tools/tempo/`, protocol firmware in
`fw/`, RTL in `src/`, and the CPython/MicroPython host library in
`host/tempo_host/`. After firmware changes, regenerate the host images with
`python tools/gen_images.py` from the repository root.

## Use the chip

The [datasheet](docs/info.md) has the pin map, host protocol and a demo-board
UART example. [Use cases](docs/use-cases.md) cover wiring, commands, rates
and current limits. The [architecture](docs/architecture.md) specifies the ISA and
lane registers. [Decisions](docs/decisions.md) record the design tradeoffs.

The host SPI port loads programs entirely through Tiny Tapeout's standard
pins. The demo-board transport uses the RP2350's hardware SPI0; a serial
bridge provides PC access. No dedicated UART, SPI or I2C peripheral fixes
which protocols the chip can run after fabrication.

Licensed under [Apache-2.0](LICENSE).
