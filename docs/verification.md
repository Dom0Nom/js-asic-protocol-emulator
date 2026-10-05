# Verification

## Method

Tempo has one executable specification: a cycle-exact Python model of the whole chip ([sim.py](../tools/tempo/sim.py), [lane.py](../tools/tempo/lane.py), [host.py](../tools/tempo/host.py)) that predicts every pad on every cycle. Instruction encodings live in one table, [isa.py](../tools/tempo/isa.py), which generates `src/tempo_isa.vh` and drives the assembler and the model's decoder. Every other layer checks the RTL or the firmware against this model, or against a model written independently from a protocol specification.

## Layers

### RTL-versus-model lockstep

[test/lockstep.py](../test/lockstep.py) runs the RTL (cocotb, Icarus) and the model on one simulated board. Each cycle both receive the same 16 input pads, and the test compares:

- the value of all 16 output pads (`uo_out`, and `uio_out` masked by its output enable);
- the 8 output enables (`uio_oe`);
- whether an instruction committed, and if so its thread, its PC, whether it wrote a register, which register, and the value.

The first mismatch stops the test. Programs are loaded through the host SPI port. The clock period comes from `info.yaml` through [clock.py](../tools/tempo/clock.py).

Not compared: internal lane state, flags and scratch memory, except as they reach a pad or a register write. At gate level the commit comparison is off, because the netlist has no commit port.

### Constrained-random programs

[randprog.py](../tools/tempo/randprog.py) generates four-thread programs and [test/test_random.py](../test/test_random.py) runs them in lockstep. The generator draws from every instruction class, including lane configuration, external-clock receive, self-modifying stores and, rarely, illegal operations on the removed lane 3. Its constraints:

- control flow only goes forward, so every program ends in `halt` or a bounded wait;
- lane slot lengths and prescalers stay small, so many slots fit in a run;
- blocking instructions (`rx`, `hget`, `wev`) sit behind a guard with a deadline.

Random levels toggle on the dedicated inputs and a random host issues mailbox and status transactions. Coverage is the retired count per instruction name; the test logs any instruction that never retired.

Guarded blocking instructions never block. [test/test_blocking.py](../test/test_blocking.py) covers blocking with four directed lockstep tests and a biased generator (`randprog.blocking_program`). [blockcov.py](../tools/tempo/blockcov.py) counts, for `wev`, `hget`, `rx` and `hput`: immediate retirements, blocked cycles, wakes, wake latency, and episodes with several threads blocked on one source.

There is no functional coverage model of lane configurations.

### Formal (SymbiYosys)

All jobs use `smtbmc` with bitwuzla unless stated.

**Core invariants, proven.** The `ifdef FORMAL` block in [src/tempo_core.v](../src/tempo_core.v) asserts, and `formal/core.sby` task `prove` proves by k-induction:

- a thread's eight registers and its Z, C and N flags change only in its own execute slot;
- a thread's PC changes only in its own execute slot or by a host PC write to that thread;
- both mailbox counts stay at or below 4;
- `hget` never commits on an empty mailbox and `hput` never on a full one;
- the host port never pushes into a full mailbox or pops an empty one.

The lanes are replaced by [formal/lane_stub.v](../formal/lane_stub.v), whose outputs are free solver inputs, so the result holds for any lane behaviour. Instruction semantics are not proven.

**Nonvacuity.** `formal/core_cover.sby` adds nine cover statements ([formal/core_nv.vh](../formal/core_nv.vh)) to a generated copy of the core. They reach each asserted event. Threads start only after a host SPI command, so the depth-30 `bmc` task of `core.sby` ends before any instruction executes and is not counted as evidence.

**Lane start-time anchor, bounded.** [formal/lane_anchor.sv](../formal/lane_anchor.sv) pushes four commands (pins, slot length, `start T`, `emit v`) into one lane at times the solver chooses. It asserts that the level is never on the pin before cycle T, that it first appears at T unless the lane raised `LATE`, and that it appears at T without `LATE` whenever the emit was queued by T-2. BMC to depth 40, with five covers.

**Lane timing independence, bounded.** [formal/lane_miter.sv](../formal/lane_miter.sv) instantiates two copies of a full lane with the same inputs and the same six-entry command tape. The solver chooses each copy's push times independently. Once both frames have begun and neither copy reports `LATE` or `UNDER`, the data, auxiliary and clock drive states and the pin ownership must be equal on every cycle. The tape is constrained: `FRAME` on, `start T`, three entries of transmit, emit or configuration, then the end-of-frame emit; bit counts 1 to 4, slot lengths at most 3, no prescaler.

This is not a proof. BMC finds no counterexample through 14 steps. k-induction fails (`formal/lane_miter_prove.sby` task `kind`); the miter needs strengthening invariants. PDR (task `pdr`) times out. Two covers show the assertions are not vacuous: both copies finish the tape, with different push schedules and a low and a high payload level on the pin. The miter does not cover receive, capture, prescaling, longer tapes or the chip's pad resolver.

### Independent wire models

[tools/tempo/models/](../tools/tempo/models/) holds `uart`, `spi`, `i2c`, `usb_ls`, `jtag`, `swd`, `linecode` (Manchester), `ps2`, `can` and `siggen` (signal sources). They were written from the protocol specifications, not from the RTL or the firmware, and use no third-party protocol library. Each sees only pad levels and records violations in a list the tests assert is empty. Examples:

- I2C: tLOW, tHIGH, tSU;DAT, tHD;STA, tSU;STA, tSU;STO and tBUF against UM10204, and SDA changes while SCL is high.
- SPI: MOSI setup and hold, chip-select framing.
- USB low speed: SYNC, NRZI, bit stuffing, CRC5, CRC16, EOP.
- PS/2: clock period and phase limits, data setup and hold, parity, stop bit.
- CAN: bit stuffing, CRC-15, ACK, fixed-field form, arbitration.

A defect shared by the cycle model and the RTL passes lockstep and still fails here. The models have their own tests with negative cases (`tools/tests/test_models_*.py`, and checker cases in `test_lane_ps2.py`, `test_lane_can.py` and `test_demos.py`). The models work on whole clock cycles; they do not check voltage, edge rate or pad delay.

### Firmware and host-library tests

Each row is firmware under `fw/`, most driven by the host library in `host/tempo_host/`. "Model" runs it on the cycle model under pytest (`tools/tests/`). "RTL lockstep" runs it on the RTL with the lockstep comparison and the wire models attached (`test/`).

| Protocol or tool | Model | RTL lockstep | Gate level |
|---|---|---|---|
| UART | `test_lane_uart.py`, `test_host_uart.py` | `test.py`: `test_host_boot_and_uart_tx`, `test_uart_rx_fast_sender` | {{GL}} |
| SPI controller, modes 0-3 | `test_lane_spi.py`, `test_host_spi.py` | `test.py`: `test_spi_mode0_jedec`, `test_spi_mode3_flash_read`; `test_usecases.py`: `test_spi_all_modes_at_minimum_period` | {{GL}} |
| I2C controller with clock stretching | `test_lane_i2c.py`, `test_host_i2c.py` | `test.py`: `test_i2c_write_read_with_stretching`; `test_usecases.py`: `test_uc2_i2c_bringup` | {{GL}} |
| I2C target (24C02) | `test_host_target.py` | `test_usecases.py`: `test_uc7_i2c_eeprom_target` | {{GL}} |
| USB low-speed packet PHY | `test_lane_usb.py` | `test.py`: `test_usb_ls_rx_and_tx` | {{GL}} |
| PS/2, device transmit and host receive | `test_lane_ps2.py` | `test_ps2.py` | {{GL}} |
| CAN 2.0A transmit with arbitration | `test_lane_can.py` | `test_can.py` | {{GL}} |
| Manchester (`fw/manchester.s`) | `test_demos.py` | `test_usecases.py`: `test_demo_manchester_frame_scheduled_across_counter_wrap` | {{GL}} |
| JTAG chain scan and pin finder | `test_host_jtag.py` | `test_usecases.py`: `test_uc4_jtag_chain_and_pin_finder` | {{GL}} |
| SWD attach | `test_host_swd.py` | `test_usecases.py`: `test_uc5_swd_attach` | {{GL}} |
| Logic analyser | `test_host_la.py` | `test_usecases.py`: `test_uc8_logic_analyser` | {{GL}} |
| I2C and SPI bus monitors | `test_host_monitor.py` | `test_usecases.py`: `test_uc6_i2c_monitor`, `test_uc6_spi_monitor` | {{GL}} |
| Triggered pulse | `test_host_trigger.py` | `test_usecases.py`: `test_uc9_triggered_pulse` | {{GL}} |
| UART autobaud | `test_host_uart.py` | `test_usecases.py`: `test_uc1_uart_autobaud_console` | {{GL}} |
| Three protocols at once (`fw/demo_multibus.s`) | `test_demos.py` | `test_usecases.py`: `test_demo_resident_multibus_with_busy_host_and_memory` | {{GL}} |

The tests include senders 1.5% to 2% off rate (UART, USB), SPI at its minimum period (6 cycles in modes 0 and 2, 7 in modes 1 and 3), and CAN arbitration lost and won. `test_isa.py` round-trips all 65,536 instruction words and fails if the Verilog header is stale.

### Gate level

`make -B GATES=yes` in `test/` runs the same cocotb tests on the routed netlist with the IHP cell models. `tools/gl_run.sh` runs named tests against a given netlist and, with `SDF=<file>`, back-annotates the flow's SDF for a corner. The pad comparison and the wire models stay active.

Limits: the simulator is Icarus. The SRAM is a behavioural model without delays. Icarus does not evaluate SDF timing checks, so an SDF run shows function at annotated delays, not setup or hold.

### Physical signoff

The Tiny Tapeout LibreLane flow checks static timing after routing at three corners (typical; slow, 1.08 V, 125 C; fast, 1.32 V, -40 C), routing DRC, KLayout DRC, LVS, antenna rules and power-grid connectivity. The Tiny Tapeout precheck then runs on the GDS. A per-block yosys run (`tools/synth.sh`) fails on inferred latches and conflicting drivers.

Magic DRC is off, because Magic reports false overlaps on the SRAM abstract.

## Results

<!-- results:begin -->
<!-- results:end -->

## Measured benefit of lanes

This compares two ways of sending an 8N1 UART stream from one Tempo thread: a software loop that times pin writes with `waitt`, as a CPU with fast GPIO does, and a loop that feeds a lane ([fw/bench_swuart.s](../fw/bench_swuart.s), `tools/bench_swlane.py`). The numbers are asserted in [test_bench_swlane.py](../tools/tests/test_bench_swlane.py). They come from the cycle model, with no pad delay; the two-UART case at 217 and 250 cycles per bit also runs on the RTL in lockstep. This is not a comparison against PIO or PRU silicon.

| Measurement | Software loop | Lane |
|---|---|---|
| Instructions per byte, 25 cycles per bit | 49 | 5 |
| Worst edge error, 25 cycles per bit | 3 cycles (12% of a bit) | 0 |
| Worst edge error, 115200 baud | 3 to 3.2 cycles | under 1 cycle <!-- CLOCK --> |
| Shortest working bit period | 20 cycles | 2 cycles |
| Two UARTs from one thread, 217 and 250 cycles per bit | decodes; deadlines missed; edges off by 31 and 56 cycles | edge error 0 and 0 |
| Two UARTs from one thread, 40 and 48 cycles per bit | neither decodes | both within 2% of a bit |

A thread executes once every four cycles, so software edges land up to 3 cycles off unless the bit period is a multiple of 4. Load on the other three threads moves no edge in either method.

## Bugs the method caught

| Bug | Caught by |
|---|---|
| RTL host-port strobes took effect one cycle late, so a thread's `hget` missed its slot | Lockstep |
| The random generator never retired `call`, `djnz`, `jr`, `rx`, `hget`, `wev` or `ldi` | Random-test coverage report |
| Every `emit` ended a frame, so a late command started whenever it arrived, with no flag | Formal, lane miter counterexample |
| The depth-30 core BMC never executed an instruction | Formal, cover statements |
| A fast USB sender's edge re-synchronised a finished receive, which slipped a bit; model and RTL agreed | Independent USB model |
| A clocked receive cut the clock's second edge; read data arrived one bit late | Independent SWD model |
| The idle lane forced the clock pin to its idle polarity, pulling SCL low after every STOP | Independent I2C model |
| The UART prescaler ignored the 13-bit sample field; a 2% slow sender corrupted low baud rates | Independent UART model with rate error |
| The SPI host API accepted periods too short for the mode and returned stale MISO | Independent SPI target, period sweep |
| USB receive firmware blocked on the host mailbox while the lane FIFO overflowed | RTL firmware test |
| A shared loop variable inferred latches with conflicting drivers; Icarus and Verilator accepted it | Per-block synthesis check |
| The gate-level source list omitted the IHP primitive library; no netlist had ever compiled | Gate level, first run |
| Cutting power columns around the SRAM also cut their port rectangles | Tiny Tapeout precheck |
| Every receive re-initialised NRZI and destuffing state, which is wrong inside one USB packet | Review against the specification |

Constrained-random lockstep has found no RTL defect; its row is a testbench defect.

## AI-assisted verification

The RTL, the cycle model, the firmware, the wire models and the tests were written with an AI coding assistant (Claude) under the author's direction. Four things make that trustworthy. The wire models and firmware tests were written from the protocol specifications and the [architecture document](architecture.md), not from the RTL. The RTL is checked against the model on every cycle, not by inspection. Independent review passes, each started with fresh context, audited the claims in this document against the run logs. The table above records which layer caught which defect, including defects in the model, the testbench and the flow.

## Gaps

- The lane timing-independence miter is bounded and covers a constrained tape family. The anchor property is bounded too. Only the core invariants are proven.
- SDF gate-level runs skip timing checks and the SRAM delay. Gate level runs a subset of the tests.
- No silicon and no FPGA run. No test includes pad delay.
- USB low speed is a packet PHY only, with no enumeration. CAN is transmit only. PS/2 has no host-to-device direction.
- Host-side call costs are modelled as fixed delays, not measured on an RP2350.
- 10BASE-T is unsupported.
- The flow reports maximum-slew, maximum-capacitance and maximum-fanout warnings; post-route design repair is disabled.
- The GitHub `gds`, `precheck` and gate-level workflows have not run on this design; the flow ran locally.
