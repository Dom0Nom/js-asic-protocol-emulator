# Verification

Tempo's intended timing guarantee applies to an anchored lane frame with
FRAME remaining enabled through the final EOF, the same initial lane state,
input trace and ordered command tape, and no LATE/UNDER errors. Starting commands
from idle without an anchor can depend on firmware delivery. The all-mode
guarantee remains unproven. The methodology checks the intended guarantee
at several levels, each with a different failure mode it catches. Real
defects and the layer that found them are summarised in
[Bugs the method caught](#bugs-the-method-caught).

An explicit model counterexample checks the omitted-anchor case: both
programs issue the same PINS/LEN0/emit lane commands, but five extra CPU
NOPs move the uo0 edge from cycle 27 to 47 with zero lane sticky flags
and no illegal instructions.
That is intended unanchored behavior, and contradicts the former broader
documentation claim. The current lane has fixed-width counters; the miter
does not implement the narrowed-width abstraction proposed in early notes.

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

At the harden commit `b4b9fe8` the RTL suite passes 27/27 (2026-10-03) and
the Python suite passes 194 with 8 MicroPython skips; a clean clone of the
source export passes 196/196. See
[Current evidence at the harden commit](#current-evidence-at-the-harden-commit).
These are simulation results; board verification remains pending. Current
limitations are listed under [Not done yet](#not-done-yet).

### 5. Constrained-random lockstep

`tools/tempo/randprog.py` generates random four-thread programs (all instruction classes, random lane configurations, bounded waits, forward control flow) and `test/test_random.py` runs them in lockstep with random noise on the inputs; unused bidirectional pins loop the chip's outputs back into its inputs, so lane receive paths see real traffic. At the harden commit `b4b9fe8`: 100 seeds x 3000 cycles, 22,539 commits compared, no divergence. (An earlier run, 2026-09-26, also recorded 3,377 external-clock receive samples.) Every instruction name has nonzero retired coverage; rare blocking instructions remain sparsely covered (`wev`: 1, `hget`: 5). The random run is repeated after every RTL change.

### 6. Formal proofs (SymbiYosys)

- `formal/lane_miter.sv`: a bounded timing-independence miter for one full
  lane. Two copies receive the same six-entry tape with independently
  chosen push times. Entry 0 enables FRAME, entry 1 anchors START at cycle
  4..24, entries 2..4 are TX/emit/config, and entry 5 emits EOF. Once both
  frames have begun and neither reports LATE/UNDER, data/aux/clock drive
  states and ownership must match. TX counts are 1..4, echo receive is
  disabled, pins remain at reset NOPIN, and configured lengths/offsets are at most 3.
  PRE is zero, capture is disabled and input filtering is off. It does not
  cover general RX, prescaling, capture, pin reassignment or longer tapes.
  Comparisons are lane drive states/ownership, not the chip's pad resolver.
- `formal/core.sby`: barrel isolation (a thread's registers, flags and PC change only in its own execute cycle, or by a host PC write) and mailbox bounds, with the lanes replaced by unconstrained stubs so the properties hold for any lane behaviour.

Results:

- `core.sby`, task `prove`: barrel isolation, mailbox bounds, valid simultaneous mailbox indices proven by k-induction (depth 8, bitwuzla), PASS at the harden commit `b4b9fe8` (2026-10-03). The proof covers only these asserted core invariants, with lanes replaced by unconstrained stubs, so they hold for any lane behaviour. Task `bmc` (depth 30) passes but is vacuous over thread execution: threads cannot start before step 36 (bug 53).
- `lane_miter.sv`: the latest run (2026-10-03, 7,200 s) found no counterexample through step 15 and timed out at step 16. The timing-independence theorem remains unproven; it is not a passing depth-60 result.
- `lane_anchor.sv` defines a separate short anchored-emit property and
  on-time/late cover goals. Its safety result is BMC depth 40 (bounded, not
  a proof); BMC and its five covers PASS at the harden commit `b4b9fe8`.
  The miter now declares completion goals requiring
  both full tapes to finish, at least one TX, enabled-high lane output and
  no LATE/UNDER. The stronger goal requires a TX/LW/TX payload, distinct
  enqueue schedules specifically for entries 2..4, and enabled low/high
  payload states observed before either final EOF is enqueued. Safety
  assumptions remain unchanged; these extra conditions are cover goals.
  The initial TX/TX/LW goal was unreachable: reset symbols made both TXs
  low, and the trailing LW could not configure high payload. The corrected
  TX/LW/TX goal has a deterministic two-copy RTL trace with low/high payload,
  differing push schedules, complete tapes and no LATE/UNDER or drive
  mismatch (the checker below writes its logs to `runs/formal_miter_trace/`). Run
  `.venv/bin/python formal/check_miter_trace.py` to reproduce both the old
  tape's missing strong cover and the corrected tape's completed covers.
  The checker compiles pre-run source snapshots and rejects concurrent
  changes. Its generated simulation constraint block uses `always_comb`
  to evaluate constant tape assumptions at time zero; a negative trace
  must fail these assumptions despite otherwise completing both covers.
  This demonstrates reachability
  for that concrete schedule, not solver coverage or a general safety proof.
  The cover task also now shares the configured solver engine with BMC;
  the pinned SBY parser confirms both task selections.
  Task-tag behavior is described in the
  [SBY reference](https://symbiyosys.readthedocs.io/en/latest/reference.html#tasks-section).
  Solver witnesses for both miter covers (`cover`, `cover_hint`) PASS at the
  harden commit `b4b9fe8`; see also the history in "Formal witnesses and
  nonvacuity, 2026-09-30" below. Safety remains
  bounded: no counterexample through step 15 (2026-10-03).

Run with `sby -f formal/<job>.sby [task]` (SymbiYosys, `smtbmc bitwuzla`); `core_cover.sby` builds its instrumented core copy itself through `formal/core_nv_gen.sh`.

### 7. Synthesis and physical gates

`tools/synth.sh` synthesises any block against the IHP liberty and fails on inferred latches or conflicting drivers. Full hardens run LibreLane through the Tiny Tapeout flow (`.github/workflows/gds.yaml`), followed by the Tiny Tapeout precheck.

The SRAM PDN step has unit tests for interval subtraction and LEF obstruction
parsing. A pinned-flow probe checks the resulting OpenDB geometry independently:
the old database has 32 Metal4 strap/pin shapes crossing SRAM obstructions,
the clipped database has zero. PSM reports zero VPWR/VGND connectivity
violations. This is geometry/connectivity evidence, not a new GDS, DRC or LVS
signoff result.

Macro timing selection has three regressions using the actual IHP corner
names. LibreLane's view filter also selects the matching supplied macro
files in a resolved experiment config. This catches use of a typical
SRAM view at fast/slow corners; it does not establish timing closure.

`tools/tests/test_transport.py` checks that assumed host
call costs advance the live chip at its actual clock while preserving
wire data. `tools/bench_rates.py` checks sustained SPI/UART transfers across
host-cost scenarios; those are sensitivity predictions, not board timings.

## Physical signoff

Harden of `b4b9fe8` (chip sources identical to `835c1d1`; `final/commit_id.json` names `b4b9fe8`), 2026-10-01, Tiny Tapeout flow with tt-support-tools `d66cf17`, LibreLane 3.1.0.dev3 and IHP-Open-PDK `2bbec75`, 6x4 tiles, `CLOCK_PERIOD` 40 ns. All values from `final/metrics.csv` and the step reports of that run.

| Check | Result |
|---|---|
| Setup, reg-to-reg worst slack | typ +16.89 ns, slow (1.08 V, 125 C) +3.09 ns, fast +24.78 ns; 0 violating paths at every corner |
| Hold, worst slack | typ +0.28 ns, slow +0.58 ns, fast +0.083 ns; 0 violations |
| Detailed-route DRC | 0 after 10 iterations |
| KLayout DRC, LVS, antenna | 0, 0, 0 |
| Power grid (VPWR / VGND, checked after the SRAM columns are connected) | 0 / 0 |
| Illegal overlaps | 0 |
| Lint errors, inferred latches | 0, 0 |
| Tiny Tapeout precheck | 9 of 9 checks pass on the same GDS (sha256 `e25dd430...`) |
| Max slew / max cap / max fanout | 121 slow, 37 typ, 0 fast / 13 per corner / 13 per corner |
| Disconnected pins | 1, the unused `ena` input (0 critical) |

The electrical items are open, not signed off. Slew and cap violators are 1x NAND/OAI drivers on wide select nets (worst 5.07 ns against the 2.51 ns library limit, so the delay calculator extrapolates past the table) and one SRAM data output; the fanout violators are the four top clock buffers (16 sinks each against a limit of 8) and nine data buffers (9 to 11). OpenSTA at the slow corner on the final netlist and extracted parasitics finds that every worst setup and hold path through any of the 135 violator pins keeps at least 1.42 ns of slack. The SDF gate-level runs below use the same delays. Post-route design repair, which targets these, hung in an earlier harden and is disabled.

The PSM-0069 lines in the harden's `error.log` come from `OpenROAD.GeneratePDN`, which runs before `Project.ExtendPowerStripes` connects the SRAM power columns; the post-connection check reports 0 VPWR/VGND violations and the final IR-drop step reports all VPWR/VGND shapes connected.

## Current evidence at the harden commit

Every run below used the exact source of the harden commit `b4b9fe8` (or
`835c1d1`, whose `src/`, `test/`, `tools/`, `fw/` and `formal/` are identical
to it); each log's first line names its commit and reports zero modified
tracked files.

| Check | Result |
|---|---|
| RTL cocotb suite (`cd test && make -B`), 27 cases | 27/27 PASS, 2,645 s (Icarus 14.0 on the Linux build machine; the same suite passed 27/27 on macOS at `d792435`, same RTL and tests) |
| Constrained-random lockstep, 100 seeds x 3000 cycles | PASS, 22,539 commits matched RTL against the model, 507 s |
| Python model, assembler, host and benchmark tests | 194 passed, 8 skipped (the MicroPython-port tests; that machine has no `micropython`), 975 s. A clean clone of the source export (which leaves out the 6 export-tool tests) passes all 196 of its tests on macOS, the MicroPython ones included, plus the UART, SPI and I2C cocotb smoke cases |
| Gate level, routed `nl.v` of the harden, zero delay | 10/10 PASS: `test_host_boot_and_uart_tx`, `test_uart_rx_fast_sender`, `test_uc1_uart_autobaud_console`, `test_spi_mode3_flash_read`, `test_spi_mode0_jedec`, `test_uc3_spi_flash_programmer`, `test_i2c_write_read_with_stretching`, `test_uc2_i2c_bringup`, `test_demo_resident_multibus_with_busy_host_and_memory`, `test_demo_manchester_frame_scheduled_across_counter_wrap` |
| Gate level with SDF, 40 ns clock | `test_host_boot_and_uart_tx`, `test_spi_mode3_flash_read`, `test_i2c_write_read_with_stretching`: 3/3 PASS at the slow corner (1.08 V, 125 C) and 3/3 PASS at the fast corner (1.32 V, -40 C); 27,571 of 27,572 SDF cell entries annotated (SRAM macro dropped) |
| `core.sby` `prove` (k-induction, depth 8) | PASS, 5 s: barrel isolation and mailbox bounds, unbounded |
| `core.sby` `bmc` (depth 30) and `bmc_deep` (depth 50) | PASS, 61 s and 311 s; depth 30 is vacuous for thread execution (bug 53), depth 50 bounds about 14 cycles of execution |
| `core_cover.sby` `cover` and `cover_host` | PASS, 800 s: 5 of 5 thread covers (steps 36-39); PASS, 2,919 s: 4 of 4 host covers (steps 48, 66, 81, 82) |
| `lane_anchor.sby` `bmc` (depth 40) and `cover` | PASS, 341 s; PASS, 5 of 5 covers |
| `lane_miter.sby` `cover` and `cover_hint` | PASS, both covers; PASS, both covers |
| `lane_miter.sby` `bmc` (depth 60) | No counterexample through step 15 (one step beyond the 2026-09-30 run); timed out at 7,200 s while checking step 16. Bounded, not a proof |
| Lane miter, unbounded attempts (`formal/lane_miter_prove.sby`, tasks `kind`, `pdr`) | k-induction (depth 24) fails: the induction step finds an unreachable counterexample state, so the miter needs strengthening invariants. `abc pdr` (after `flatten; memory_map; setundef -anyseq`, which fixes the earlier "x or z bits" AIG export error, rc 16): no proof and no counterexample after 7,200 s, frame 9. The theorem remains unproven |

Scope of the SDF runs: Icarus annotates every standard-cell and interconnect
delay entry except the SRAM macro (dropped; its 7.1-7.3 ns read is covered by
STA). It also prints 68 "sorry: ifnone with an edge-sensitive path is not
supported" messages, for 17 multi-path cell types (`a221oi`, `a22oi`, `a21o`,
`a21oi`, `o21ai`, `xor2`, `xnor2`, `mux2`, `mux4` and the scan flops): for
those cells it skips the default (`ifnone`) arc, so an input change that
matches none of the cell's conditional arcs propagates without that arc's
delay. The plain reset flops (`dfrbp`) are not affected. Icarus also does not
evaluate TIMINGCHECK entries. The SDF runs therefore show function at the
annotated corner delays, partly optimistically; setup and hold signoff is
static timing analysis.

## Bugs the method caught

The project bug log holds 54 real defects in the model, RTL, firmware, host
library, flow and verification code, each with the check that exposed it.
Testbench-only defects are logged separately and not counted here.

| Caught by | Count | Bugs |
|---|---|---|
| Formal: timing miter and nonvacuity covers | 2 | 16, 53 |
| RTL-vs-model lockstep | 1 | 7 |
| Independent protocol models, use-case and host-library tests | 29 | 6, 8, 11, 12, 14, 15, 17, 20-28, 30-33, 37-43, 50, 51 |
| Gate-level simulation | 1 | 52 |
| Lint and per-block synthesis | 3 | 5, 9, 10 |
| Review and independent audit: RTL against the model, writing firmware and host code against the spec, critic review, pin-map and tooling audits | 14 | 1-4, 13, 18, 19, 34, 36, 45-49 |
| Physical flow audits and Tiny Tapeout precheck | 4 | 29, 35, 44, 54 |

Constrained-random lockstep found no logged RTL defect; its coverage report
did find that the generator never retired `call`, `djnz`, `jr`, `rx`,
`hget`, `wev` or `ldi` (a testbench defect, since fixed).

Notable examples:

- 16 (formal): every `emit` ended a frame, so pin timing depended on firmware timing. The miter gave a counterexample at step 8; frames now end only on an `emit` with bit 7 set.
- 53 (formal covers): the depth-30 core `bmc` task was vacuous, because threads cannot execute before step 36. Covers at depth 30 reached nothing; at depth 90 they reach thread execution at steps 36-39.
- 12 (protocol model): USB receive at +1.5% clock error slipped a bit after a fast sender's edge re-synced a finished command. RTL and model agreed, so lockstep passed; only the independent USB check failed.
- 10 (synthesis): one `integer` loop variable shared by a combinational and a sequential block made yosys infer latches with conflicting drivers. Icarus and Verilator lint passed it; `tools/synth.sh` now fails on it.
- 52 (gate level): `test/Makefile` left out the IHP primitive library, so no gate-level netlist had ever compiled.
- 54 (precheck): cutting VPWR columns around the SRAM also cut their port rectangles; the Tiny Tapeout pin check failed with 32 LEF errors on an otherwise clean harden.

## Not done yet

Physical-board tests (no silicon or FPGA board run), USB enumeration (only the
low-speed packet PHY is tested), an unbounded lane timing-independence proof
(bounded BMC only), and the open electrical items in
[Physical signoff](#physical-signoff).
Random lockstep logs report retired-instruction and external-clock receive
coverage; an absent instruction remains an explicit coverage gap.

The composed firmware demo uses independent UART/I2C wire models and a
Manchester checker that compares every half-bit cycle. It explicitly
requires a UART frame and Manchester payload to overlap an I2C target's
clock stretch inside the observed START/STOP interval. This replaced a
weak final-data-only check: at 100 us host cost, the original single
Manchester frame finished before I2C started. The demo now repeats frames.
Negative checker cases reverse a whole bit, shift every midpoint, inject
a one-cycle glitch and add a high level in the required idle window.
Standalone Manchester also checks the queued start against NOW16 across
wrap, accounting for the registered pad and prior-cycle device observation.
No assertion here establishes electrical compliance or an unbounded
timing-independence theorem.

## Blocking-instruction coverage, 2026-09-30

*History: run before the `b4b9fe8` harden and not part of the runs in [Current evidence at the harden commit](#current-evidence-at-the-harden-commit).*

The plain random generator only emits `wev`, `hget` and `rx` behind a
`wany` guard, so they retire only when their source is already ready. A
model-only replay of the recorded 100-seed configuration
reproduces the RTL run's retire counts
(`wev=1 hget=5 rx=17`) and shows that none of
them ever blocked: 0 blocked cycles, 0 wakes.

`test/test_blocking.py` adds RTL-vs-model lockstep cases for the blocking
instructions (`wev`, `hget`, `rx`, and `hput`). `tools/tempo/blockcov.py`
counts, on the model side, immediate retirements (source already ready),
blocked cycles, wakes after blocking, wake latency (cycles from the source
becoming visible to the retirement; 0 means the waiter retired in the
first cycle the event was visible), and episodes with two or more threads
blocked on the same source.

- Directed: `wev_phase` (wakes from each consumer slot phase, delay walk
  0 to 15 cycles, acks pending and not), `wev_multi` (three threads blocked
  on one event; asserts each `sev` wakes exactly one; repeated `sev` on a set
  bit; self-set pending events 5 and 7), `hget` (all four threads blocked,
  bursts of 1 to 12 words through a full H2T, T2H full so `hput` blocks;
  asserts every word comes back), `rx` (two threads blocked on one lane,
  flags steer branches so a flag mismatch is a PC mismatch, RX FIFO overrun
  asserted through `stat`).
- Biased random: `randprog.blocking_program(seed)`, one or two producer
  threads (`sev`, `rxn`, `hput` after random delays) and unguarded consumers
  on small per-seed event and lane sets, write-heavy random host traffic.

Retired counts (immediate + woke), all RTL runs passed:

| Run | wev | hget | rx | hput |
|---|---|---|---|---|
| Before: plain random, 100 seeds x 3000 cycles | 1 (0 woke) | 5 (0 woke) | 17 (0 woke) | 73 (0 woke) |
| Directed, 4 cases | 194 (155 woke) | 66 (8 woke) | 92 (48 woke) | 66 (62 woke) |
| Biased, seeds 1-20 x 1500 | 130 (48 woke) | 54 (54 woke) | 85 (38 woke) | 81 (12 woke) |
| Biased, seeds 101-140 x 3000 | 521 (201 woke) | 215 (187 woke) | 373 (97 woke) | 226 (69 woke) |

Multi-waiter episodes (directed + both biased runs): wev 65, hget 149,
rx 53, hput 49. Wake latency 0 was hit for all four (wev 142, hget 100,
rx 91, hput 87). `wev` wake latency never exceeds 2 by construction: a
thread's own `sev` cannot wake itself, so latency 3 is unreachable.
In the first directed run the
lockstep matched, but the testbench's own scenario threshold for immediate
`hget` failed (1 of 5) because thread 0 drained H2T; thread 0 now delays
before `hput` and the host drains T2H when H2T is full.

Run from `test/`: `make -B COCOTB_TEST_MODULES=test_blocking` (directed
plus `BLOCK_SEEDS`, default 20, x `BLOCK_CYCLES`, default 1500, from
`BLOCK_BASE`, default 1). `BLOCK_SEEDS=0` runs only the directed cases.
The module is not in the default `COCOTB_TEST_MODULES`.

## Random lockstep and gate-level runs, 2026-09-30

*History: superseded by [Current evidence at the harden commit](#current-evidence-at-the-harden-commit).*

Plain random lockstep on the RTL of the earlier `c3ffca5` harden: 100 seeds x 3000 cycles, 22,539 commits matched between RTL and the Python model, pass (781 s).

Gate-level simulation had never run: `test/Makefile` left out the IHP primitive library (bug 52). With that fixed, `tools/gl_run.sh <netlist> <tag> "<tests>"` runs named cocotb cases against a netlist in a scratch copy of `test/` (`GATES=yes`, `NODUMP=yes`), with the functional SRAM model. Against a yosys netlist mapped to the IHP typ library (sha256 `06ef5ee7...`, 24,149 cell instances, pre-placement, zero-delay), all eight UART/SPI/I2C cases pass: `test_host_boot_and_uart_tx`, `test_uart_rx_fast_sender`, `test_spi_mode3_flash_read`, `test_spi_mode0_jedec`, `test_i2c_write_read_with_stretching`, `test_uc2_i2c_bringup`, `test_uc3_spi_flash_programmer`, `test_uc1_uart_autobaud_console`. This checks that synthesis preserves function; it is not the post-route netlist and carries no timing. The post-route result is recorded below when the harden completes.

## Formal witnesses and nonvacuity, 2026-09-30

*History: superseded by [Current evidence at the harden commit](#current-evidence-at-the-harden-commit).*

All jobs: SymbiYosys, engine `smtbmc bitwuzla`, run with a 900 s
timeout, one at a time. Every job here
is `mode cover`: a PASS shows the named states are reachable under the job's
assumptions (a solver witness). None of these is a safety proof, and none
changes the bounds of the safety jobs in section 6. `src/` was not modified;
the core covers are included into a generated copy of `tempo_core.v`
(`formal/core_nv_gen.sh`, `formal/core_nv.vh`).

| Job | Task | Depth limit | Result | Wall |
|---|---|---|---|---|
| `lane_miter.sby` | `cover` | 60 | PASS, both covers (step 9, step 12) | 30 s |
| `lane_miter.sby` | `cover_hint` | 45 | PASS, both covers (step 38) | 169 s |
| `lane_anchor.sby` | `cover` | 40 | PASS, 5 of 5 covers (steps 5-7) | 4 s |
| `core_cover.sby` | `cover` | 90 | Timeout at 900 s; 5 of 5 thread covers reached (steps 36-39), plus `hp_h2t_v` at step 48 in the earlier ungrouped run | 900 s |
| `core_cover.sby` | `cover_host` | 90 | Timeout at 900 s; 2 of 4 reached, 2 unresolved | 900 s |

SBY writes the witness traces (VCD and Yosys witness files) into each
job's output directory.

What each covers:

- Lane miter, TX/LW/TX cover (the strong cover, `lane_miter.sv`): reached by
  the solver with every tape field free, step 12. Solver-chosen tape:
  LW FRAME, start T=6, TX (n=1), LW reg 4 (0x4002), TX (n=1), end-of-frame
  emit. The two copies push the payload at different cycles, both drive a
  low and a high payload level while owned and enabled, both finish the tape
  and go idle, neither raises LATE or UNDER, and the equality asserts held
  along the trace. The weaker completion cover is reached at step 9. This
  closes the "solver witness pending" item: the miter's assertions are not
  vacuous for this tape family. `cover_hint` additionally pins the tape to
  the one in `formal/check_miter_trace.py` (define `MITER_HINT`, push times
  and pins free) and reaches both covers at step 38 with a solver-chosen
  schedule where copy B pushes ahead of A. The miter's safety result is
  unchanged at the time: BMC, no counterexample through step 14, timeout at
  step 15 (now through step 15, timeout at step 16; see section 6). It is
  not a proof, and it still covers only the constrained tape family.
- Lane anchor: the two existing covers (on time, late) plus three new ones:
  the emit is queued and pending while `now < T` without the level on D
  (the "never early" assert is exercised with the level pending), the
  on-time antecedent `now == T && done_at <= T-2` is reachable, and at the
  tightest margin `done_at == T-2` the level is on D and LATE is clear. The
  anchor safety result is still BMC depth 40 (not re-run; its assertions
  did not change), not a proof.
- Core (`core_cover.sby`, assumption: start from reset; lanes are free
  stubs; IMEM data and pins free): reached a thread-1 register change in its
  own execute cycle (step 36), a thread-2 Z flag change (37), a thread-3 PC
  advance with no host PC write (38), HPUT committing at `t2h_n == 3` (38),
  T2H full `t2h_n == 4` (39), a host H2T push (48), and a host PC write
  changing thread 2's PC outside its execute slot (66), which is the
  exception clause of the PC isolation assert. Not reached before the
  timeout (last completed step 66): HGET committing and a host T2H pop.
  These are unresolved, not shown unreachable.

Finding: threads only start on a host SPI control command, so no
instruction can execute before step 36 from reset. The core `bmc` task
(depth 30) therefore never executes an instruction and says nothing about
isolation under thread execution. The core `prove` task (k-induction,
depth 8) is unaffected: its induction step starts from arbitrary states, and
the covers above show that the states the isolation and mailbox asserts are
about are reachable under the same assumptions. The HGET and T2H-pop
preconditions remain without a witness.

Core invariants re-proved on 2026-09-30 on the harden RTL: `formal/core.sby` task `prove`, k-induction depth 8, `smtbmc bitwuzla`, PASS (basecase and induction), 7 s. This is an unbounded proof of the asserted core invariants because the induction step starts from any state satisfying them. The depth-30 `bmc` task adds nothing about running threads (they cannot start before step 36; bug 53).

Committed-tree reruns, 2026-09-30: from a clean worktree at `fbd3292` (header line records commit and zero dirty files), RTL suite 27/27 (2555 s) and Python suite 193/193 (915 s). `src/`, `test/`, `tools/`, `host/` and `fw/` at later commits differ only in docs and `tools/bench_rates.py`'s head fallback. Complete-source export of `fbd3292`: 165 files; in the export, image check passes, 193/193 Python tests, three cocotb protocol cases pass, `bench_rates.py` runs without git history.

Longer formal runs, 2026-09-30 (PC threads left idle by single-threaded detailed routing):

| Job | Task | Result | Time |
|---|---|---|---|
| `core.sby` | `bmc_deep`, BMC depth 50 | PASS, no counterexample. Threads can run from step 36, so this bounds isolation/mailbox asserts over about 14 cycles of execution; bounded, not a proof. | 417 s |
| `core_cover.sby` | `cover_host`, depth 90 | PASS: all 4 host covers reached, including the HGET commit and host T2H pop left unresolved earlier | 3920 s |
| `lane_miter.sby` | `bmc`, depth 60 | Timed out at 5000 s while checking step 15; no counterexample through step 14. Unchanged from 2026-09-26. | 5000 s |

The five thread covers (`core_cover.sby` task `cover`) also completed in a separate run: `DONE (PASS)`, all five reached at steps 36-39, 921 s. With these, all nine core nonvacuity covers have solver witnesses in completed runs. The lane timing-independence theorem remains bounded at 14 steps for the constrained tape family.

Gate-level lockstep scope (`test/lockstep.py`): in gate-level runs the internal instruction-commit comparison is off, because the netlist has no `commit_o` to probe (the gate-level log reports "0 commits matched"). The per-cycle pad comparison against the Python cycle model stays on (every output value and output enable, e.g. 6,881 cycles in the UART boot case), alongside the independent wire-level protocol models.

### Gate level on the routed netlist, 2026-09-30

*History: superseded by [Current evidence at the harden commit](#current-evidence-at-the-harden-commit).*

Netlist: `final/nl/tt_um_dom0nom_protoemu.nl.v` from the `c3ffca5` harden (`final/commit_id.json` names `c3ffca5`; sha256 `4a099941...`, 76,376 cell instances including fill). All ten cases pass, zero-delay, with the per-cycle pad comparison against the Python model and the independent wire models:

| Case | Protocol | Wall time |
|---|---|---|
| `test_host_boot_and_uart_tx` | host boot, UART TX | 13 s |
| `test_uart_rx_fast_sender` | UART RX, 2% fast sender | 15 s |
| `test_uc1_uart_autobaud_console` | UART console, autobaud | 822 s |
| `test_spi_mode3_flash_read` | SPI mode 3 flash | 23 s |
| `test_spi_mode0_jedec` | SPI mode 0 JEDEC ID | 21 s |
| `test_uc3_spi_flash_programmer` | SPI flash program/verify | 607 s |
| `test_i2c_write_read_with_stretching` | I2C with clock stretching | 66 s |
| `test_uc2_i2c_bringup` | I2C scan, EEPROM, RTC | 688 s |
| `test_demo_resident_multibus_with_busy_host_and_memory` | UART + Manchester + I2C + compute in one image | 681 s |
| `test_demo_manchester_frame_scheduled_across_counter_wrap` | Manchester line code from firmware | 189 s |

Timing-annotated gate level (same `c3ffca5` netlist): `SDF=<corner sdf> TEMPO_CLK_NS=40 tools/gl_run.sh ...` compiles with `-gspecify -ginterconnect`, back-annotates the LibreLane SDF (all 27,245 standard-cell and interconnect entries; the SRAM macro entry is dropped because Icarus cannot parse its bus-indexed IOPATHs and its behavioral model has no specify block, so the 7.1-7.3 ns SRAM read delay is covered by STA only) and runs at the declared 40 ns period. `test_host_boot_and_uart_tx`, `test_spi_mode3_flash_read` and `test_i2c_write_read_with_stretching` pass at the slow (1.08 V, 125 C) and fast (1.32 V, -40 C) corners; Icarus prints 68 "sorry: ifnone with an edge-sensitive path is not supported" messages (see "Current evidence at the harden commit" for what they skip), "TIMINGCHECK not supported", and "Chosen value not defined" for the header VOLTAGE/TEMPERATURE triples. Icarus does not evaluate TIMINGCHECK entries, so setup/hold are checked by STA, not by this simulation.

Final committed-tree reruns: from a clean worktree at `d792435` (after the gate-level, SDF and clock-knob test changes), RTL 27/27 and Python 193/193; later commits change only docs.
