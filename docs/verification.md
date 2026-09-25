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

Directed RTL tests (`test/test.py`, all in lockstep): host boot and UART TX at 3 Mbaud (fractional bit period); UART RX at 1 Mbaud from a 2% fast sender; SPI modes 0 and 3 against a flash model; I2C fast mode with a clock-stretching target, repeated START, write and read; USB low-speed receive at +1.5% clock error with bit stuffing and CRC16 residual check, and USB transmit of an ACK.

### 5. Constrained-random lockstep

`tools/tempo/randprog.py` generates random four-thread programs (all instruction classes, random lane configurations, bounded waits, forward control flow) and `test/test_random.py` runs them in lockstep with random noise on the inputs; unused bidirectional pins loop the chip's outputs back into its inputs, so lane receive paths see real traffic. Result so far: 118 seeds, 354,000 cycles, 27,356 commits compared, no divergence. 108 of those seeds ran before the receive-continuation change (bugs 11 to 13), so the random run is repeated after every RTL change.

### 6. Formal proofs (SymbiYosys)

- `formal/lane_miter.sv`: the timing-independence theorem for one lane. Two lanes receive the same command tape (configuration, a start time, transmits, emits, an end-of-frame emit) at times the solver chooses freely for each; the claim is that once both have begun the frame their pins are equal on every cycle unless one has raised LATE or UNDER. Bounded model check, slot lengths and offsets narrowed to at most 3 cycles.
- `formal/core.sby`: barrel isolation (a thread's registers, flags and PC change only in its own execute cycle, or by a host PC write) and mailbox bounds, with the lanes replaced by unconstrained stubs so the properties hold for any lane behaviour.

Results:

- `core.sby`, task `prove`: barrel isolation and mailbox bounds proven by k-induction (depth 8, bitwuzla, 3 s). Unbounded: they hold in every reachable state, for any lane behaviour. Task `bmc` (depth 30) also passes.
- `lane_miter.sv`: running.

Run with `tools/pc.sh formal <job.sby> [task]`, which ships `formal/` and `src/` to the PC without touching a harden in progress.

### 7. Synthesis and physical gates

`tools/synth.sh` synthesises any block against the IHP liberty on the Mac and fails on inferred latches or conflicting drivers; area per block is logged in `notes/area.md`. Full hardens run on the PC (`tools/pc.sh harden`) and in GitHub CI with the Tiny Tapeout precheck.

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

## Not done yet

Gate-level simulation of the hardened netlist, functional coverage reporting, and the formal results above.
