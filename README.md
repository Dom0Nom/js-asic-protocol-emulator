# Tempo protocol emulator

Tempo is a programmable protocol processor for the [Jane Street ASIC
competition](https://blog.janestreet.com/protocol-emulator-asic-competition/),
targeting IHP 130 nm CMOS5L in 6×4 Tiny Tapeout tiles.

Four threads share a 16-bit datapath and a 1024-word SRAM. Two full lanes and
one lite lane move pins on independent timelines. Firmware supplies words,
symbols and deadlines; lanes handle sampling, clock pulses and line coding.
This lets one instruction set implement UART, SPI, I2C, JTAG, SWD, passive
monitoring and edge capture. NRZI, stuffing and CRC are reusable settings;
USB low-speed PHY tests exist, but USB enumeration is unfinished.

The verification compares every RTL pad and instruction commit with a Python
cycle model. Independent target models check the protocols on the wire.
Constrained-random programs and formal proofs cover the core's isolation and
mailbox bounds. See [verification](docs/verification.md) and the
[bug log](notes/bugs.md) for evidence and remaining work.

The latest completed routed design fails slow-corner timing at the 50 MHz
target. This is a design in progress; current area and timing results are in
[notes/area.md](notes/area.md).

## Run the checks

With Icarus Verilog installed and the project Python environment active:

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
UART example. The [architecture](docs/architecture.md) specifies the ISA and
lane registers. [Decisions](docs/decisions.md) record the design tradeoffs.

The host SPI port loads programs entirely through Tiny Tapeout's standard
pins. The demo-board transport uses the RP2350's hardware SPI0; a serial
bridge provides PC access. No dedicated UART, SPI or I2C peripheral fixes
which protocols the chip can run after fabrication.

Licensed under [Apache-2.0](LICENSE).
