# Verification

Tempo's claim is that pin timing is exact and independent of firmware timing. The methodology is built to check that claim at several levels, each with a different failure mode it catches. Real defects and the layer that found them are logged in `notes/bugs.md`; the table at the end summarises it.

## Layers

### 1. One executable specification

`tools/tempo/isa.py` holds every encoding. The assembler, the disassembler, the model's decoder and the RTL's `src/tempo_isa.vh` are generated from or checked against it. A test decodes and re-encodes all 65,536 words (`tools/tests/test_isa.py`), and another fails if the Verilog header is stale.

### 2. A cycle-level model of the whole chip

`tools/tempo/sim.py`, `lane.py` and `host.py` model every register of the chip at the clock edge: the barrel with its fetch slot, the lanes with their queues and timelines, the host SPI port, the input synchroniser and the registered pads. It predicts every pad on every cycle. It is the reference for the RTL and the place where firmware is developed first.

The model follows one discipline the RTL also follows: in a cycle, threads, lanes and the host port all read pre-edge state, and their updates merge at the edge by fixed rules (queue pushes land behind lane pops, an RX pop happens before a lane push, a newly set event survives a same-cycle consume, the host wins run-bit conflicts). Writing those rules down removed a class of simulation-order bugs before the RTL existed.

### 3. Independent protocol models

`tools/tempo/models/` holds UART, SPI (including a flash-like target), I2C (target and passive bus monitor with UM10204 timing checks), USB low speed (receiver with DPLL, stuffing, CRC5/CRC16 checks; transmitter with adjustable clock error) and the host SPI driver. They were written by a separate agent from the protocol specifications, not from Tempo's design, and each has negative tests showing it catches violations. They check what is on the wire, so a bug shared by the model and the RTL still fails here (bugs 12 and 15).

### 4. RTL-vs-model lockstep

`test/lockstep.py` runs the RTL (cocotb, Icarus) and the model on one simulated board. Every cycle both receive the same pad inputs; the RTL's pads must equal the model's, and every instruction commit (thread, PC, register write) must match. Board devices react to the RTL's outputs. Programs are loaded through the host SPI port, as on the demo board.

Directed RTL tests (`test/test.py`, all in lockstep): host boot and UART TX at 3 Mbaud (fractional bit period); UART RX at 1 Mbaud from a 2% fast sender; SPI modes 0 and 3 against a flash model; I2C fast mode with a clock-stretching target, repeated START, write and read; USB low-speed receive at +1.5% clock error with bit stuffing and CRC16 residual check, and USB transmit of an ACK. A register-bank test gives every thread/register a distinct word and reads all 32 back over the host port.

`test/test_usecases.py` also checks the host SPI firmware in all four modes,
both bit orders, at the cycle-model minimum periods (5 cycles for modes 0/2,
6 for modes 1/3). Non-palindromic bytes expose bit reversal and stale MISO;
a nine-byte transfer covers an odd tail and an eight-byte host chunk boundary
with CS held low. Independent targets check MOSI setup/hold, while pad and
commit lockstep checks the RTL against the cycle model. Bulk-read correctness
is checked through the SRAM double buffer in both bit orders, and a model throughput regression
requires the pipelined loop to improve on its 132,889-cycle baseline.

Host/firmware regressions also inject a data NACK at both I2C address widths
and require exactly one rejected byte followed by STOP, then a successful
new transfer. UART tests start traffic immediately when construction returns,
check data together with parity flags, and receive from a 2% slow sender at
300/600/1200/2400 baud. The RTL suite checks 300 baud and all three parity
settings. Image-switch tests retain old pin ownership, capture mode, mailbox
commands and UART ring state before loading again, and verify the resulting
wire data and flags. Numeric tests cover fractional timing carries; the
MicroPython unix port checks the same carry and sample-width boundary.
The SPI monitor is also armed during a 150-transaction burst. Its constructor
must preserve records read while waiting for readiness, every damaged record
must be flagged, and an intact transaction must arrive after the backlog drains.

The final host/firmware code passes all 24 directed/use-case RTL tests,
with no failures or skips and exit 0, on the PC (Icarus 14, cocotb 2.0.1,
Python 3.12), 2026-09-26. All 73 simulation source hashes match the working
tree. The complete Python suite also passes all 157 tests, including
MicroPython replay and CLI sessions. These are simulation results; board
verification and physical signoff remain pending. Logs and current limitations
are recorded in `notes/progress.md`.

### 5. Constrained-random lockstep

`tools/tempo/randprog.py` generates random four-thread programs (all instruction classes, random lane configurations, bounded waits, forward control flow) and `test/test_random.py` runs them in lockstep with random noise on the inputs; unused bidirectional pins loop the chip's outputs back into its inputs, so lane receive paths see real traffic. On the final retained RTL, 2026-09-26: 100 seeds, 300,000 cycles, 22,539 commits compared, no divergence, and 3,377 external-clock receive samples. Every instruction name has nonzero retired coverage; rare blocking instructions remain sparsely covered (`wev`: 1, `hget`: 5). The random run is repeated after every RTL change.

### 6. Formal proofs (SymbiYosys)

- `formal/lane_miter.sv`: the timing-independence theorem for one lane. Two lanes receive the same command tape (configuration, a start time, transmits, emits, an end-of-frame emit) at times the solver chooses freely for each; the claim is that once both have begun the frame their pins are equal on every cycle unless one has raised LATE or UNDER. Bounded model check, slot lengths and offsets narrowed to at most 3 cycles.
- `formal/core.sby`: barrel isolation (a thread's registers, flags and PC change only in its own execute cycle, or by a host PC write) and mailbox bounds, with the lanes replaced by unconstrained stubs so the properties hold for any lane behaviour.

Results:

- `core.sby`, task `prove`: barrel isolation, mailbox bounds, valid simultaneous mailbox indices proven by k-induction (depth 5, bitwuzla) on the index-width-fixed core, 2026-09-26. Task `bmc` (depth 30) also passes on the final core. Lanes are unconstrained stubs, so the core properties hold for any lane behaviour.
- `lane_miter.sv`: the last run found no counterexample through step 14 and timed out at step 15. The timing-independence theorem remains unproven; it is not a passing depth-60 result.

Run with `tools/pc.sh formal <job.sby> [task]`, which ships `formal/` and `src/` to the PC without touching a harden in progress.

### 7. Synthesis and physical gates

`tools/synth.sh` synthesises any block against the IHP liberty on the Mac and fails on inferred latches or conflicting drivers; area per block is logged in `notes/area.md`. Full hardens run on the PC (`tools/pc.sh harden`) and in GitHub CI with the Tiny Tapeout precheck.

The SRAM PDN step has unit tests for interval subtraction and LEF obstruction
parsing. A pinned-flow probe checks the resulting OpenDB geometry independently:
the old database has 32 Metal4 strap/pin shapes crossing SRAM obstructions,
the clipped database has zero. PSM reports zero VPWR/VGND connectivity
violations. This is geometry/connectivity evidence, not a new GDS, DRC or LVS
signoff result. A deliberately one-second experiment returns timeout status
124, verifying that the runner no longer hides flow failures.

## What each layer caught

From `notes/bugs.md`:

| Layer | Bugs |
|---|---|
| Writing the RTL against the model (review) | 1, 2, 3, 4 |
| Lint | 5 |
| Model-level protocol tests | 6, 11 |
| RTL-vs-model lockstep | 7 |
| Chip-level protocol tests | 8, 12, 14, 15 |
| Per-block synthesis | 9, 10 |
| Writing firmware against the spec | 13 |
| Host compatibility and independent SPI timing sweep | 30, 31, 32, 33 |
| Submission pin map against RTL and board transport | 34 |
| Independent physical metrics and obstruction audit | 29, 35 |
| Experiment runner failure-path check | 36 |
| Host/firmware negative protocol and image-switch tests | 37, 38, 39, 40, 41, 42, 43 |

## Not done yet

Full GDS signoff and gate-level simulation matching the final RTL/PDN, physical-board tests,
USB enumeration, and completion of the lane timing-independence proof.
Random lockstep logs report retired-instruction and external-clock receive
coverage; an absent instruction remains an explicit coverage gap.
