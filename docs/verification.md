# Verification

Tempo's intended timing guarantee applies to an anchored lane frame with
FRAME remaining enabled through the final EOF, the same initial lane state,
input trace and ordered command tape, and no LATE/UNDER errors. Starting commands
from idle without an anchor can depend on firmware delivery. The all-mode
guarantee remains unproven. The methodology checks the intended guarantee
at several levels, each with a different failure mode it catches. Real
defects and the layer that found them are logged in `notes/bugs.md`.

An explicit model counterexample checks the omitted-anchor case: both
programs issue the same PINS/LEN0/emit lane commands, but five extra CPU
NOPs move the uo0 edge from cycle 27 to 47 with zero lane sticky flags
and no illegal instructions (`runs/goal_unanchored_counterexample.json`).
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

The bug-fix source state at `376c03a` passes all 24 directed/use-case RTL tests,
with no failures or skips and exit 0, on the PC (Icarus 14, cocotb 2.0.1,
Python 3.12), 2026-09-26. All 73 simulation source hashes matched that
checkout. Its complete Python suite also passes all 157 tests, including
MicroPython replay and CLI sessions. These are simulation results; board
verification and physical signoff remain pending. Logs and current limitations
are recorded in `notes/progress.md`.

### 5. Constrained-random lockstep

`tools/tempo/randprog.py` generates random four-thread programs (all instruction classes, random lane configurations, bounded waits, forward control flow) and `test/test_random.py` runs them in lockstep with random noise on the inputs; unused bidirectional pins loop the chip's outputs back into its inputs, so lane receive paths see real traffic. On the final retained RTL, 2026-09-26: 100 seeds, 300,000 cycles, 22,539 commits compared, no divergence, and 3,377 external-clock receive samples. Every instruction name has nonzero retired coverage; rare blocking instructions remain sparsely covered (`wev`: 1, `hget`: 5). The random run is repeated after every RTL change.

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

- `core.sby`, task `prove`: barrel isolation, mailbox bounds, valid simultaneous mailbox indices proven by k-induction (depth 8, bitwuzla), re-run PASS on 2026-09-30 (`runs/formal_0930_core_prove.log`). The proof covers only these asserted core invariants, with lanes replaced by unconstrained stubs, so they hold for any lane behaviour. Task `bmc` (depth 30) passes but is vacuous over thread execution: threads cannot start before step 36 (`notes/bugs.md` 53).
- `lane_miter.sv`: the last run found no counterexample through step 14 and timed out at step 15. The timing-independence theorem remains unproven; it is not a passing depth-60 result.
- `lane_anchor.sv` defines a separate short anchored-emit property and
  on-time/late cover goals. Its safety result is BMC depth 40 (bounded, not
  a proof); its five covers were reached by the solver on 2026-09-30
  (`runs/formal_0930_anchor_cover.log`). The miter now declares completion goals requiring
  both full tapes to finish, at least one TX, enabled-high lane output and
  no LATE/UNDER. The stronger goal requires a TX/LW/TX payload, distinct
  enqueue schedules specifically for entries 2..4, and enabled low/high
  payload states observed before either final EOF is enqueued. Safety
  assumptions remain unchanged; these extra conditions are cover goals.
  The initial TX/TX/LW goal was unreachable: reset symbols made both TXs
  low, and the trailing LW could not configure high payload. The corrected
  TX/LW/TX goal has a deterministic two-copy RTL trace with low/high payload,
  differing push schedules, complete tapes and no LATE/UNDER or drive
  mismatch (`runs/formal_miter_trace/fixed_tape.log`). Run
  `.venv/bin/python formal/check_miter_trace.py` to reproduce both the old
  tape's missing strong cover and the corrected tape's completed covers.
  The checker compiles pre-run source snapshots and rejects concurrent
  changes. Its generated simulation constraint block uses `always_comb`
  to evaluate constant tape assumptions at time zero; a negative trace
  must fail these assumptions despite otherwise completing both covers.
  This demonstrates reachability
  for that concrete schedule, not solver coverage or a general safety proof.
  The cover task also now shares the configured solver engine with BMC;
  the pinned SBY parser confirms both task selections
  (`runs/goal_miter_task_engine_check.log`).
  Task-tag behavior is described in the
  [SBY reference](https://symbiyosys.readthedocs.io/en/latest/reference.html#tasks-section).
  Solver witnesses for both miter covers were obtained on 2026-09-30 (see
  "Formal witnesses and nonvacuity, 2026-09-30" below; traces in
  `runs/formal_0930_witness/`). Safety remains bounded at step 14.

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

Macro timing selection has three regressions using the actual IHP corner
names. LibreLane's view filter also selects the matching supplied macro
files in the resolved PC experiment config. This catches use of a typical
SRAM view at fast/slow corners; it does not establish timing closure.

`tools/tests/test_public_export.py` creates disposable repositories and
checks a complete export by source bytes, firmware regeneration,
assembler/ISA execution and host imports. It also verifies that the source
checkout, unrelated repositories and unreviewed destination edits survive
rejected exports. `tools/tests/test_transport.py` checks that assumed host
call costs advance the live chip at its actual clock while preserving
wire data. `tools/bench_rates.py` checks sustained SPI/UART transfers across
host-cost scenarios; those are sensitivity predictions, not board timings.

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
| Actual corner selection and resolved LibreLane view filtering | 44 |
| Disposable complete-source export and destination regressions | 45, 46 |

## Not done yet

Full GDS signoff and gate-level simulation matching the final RTL/PDN, physical-board tests,
USB enumeration, and completion of the lane timing-independence proof.
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
timing-independence theorem; run status is in `notes/progress.md`.

## Blocking-instruction coverage, 2026-09-30

The plain random generator only emits `wev`, `hget` and `rx` behind a
`wany` guard, so they retire only when their source is already ready. A
model-only replay of the recorded 100-seed configuration
(`runs/cov_0930_baseline_model.log`) reproduces the RTL log's retire counts
(`wev=1 hget=5 rx=17`, `runs/resume_random100.log`) and shows that none of
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
| Directed, 4 cases (`runs/cov_0930_directed.log`) | 194 (155 woke) | 66 (8 woke) | 92 (48 woke) | 66 (62 woke) |
| Biased, seeds 1-20 x 1500 (`runs/cov_0930_biased.log`) | 130 (48 woke) | 54 (54 woke) | 85 (38 woke) | 81 (12 woke) |
| Biased, seeds 101-140 x 3000 (`runs/cov_0930_biased40.log`) | 521 (201 woke) | 215 (187 woke) | 373 (97 woke) | 226 (69 woke) |

Multi-waiter episodes (directed + both biased runs): wev 65, hget 149,
rx 53, hput 49. Wake latency 0 was hit for all four (wev 142, hget 100,
rx 91, hput 87). `wev` wake latency never exceeds 2 by construction: a
thread's own `sev` cannot wake itself, so latency 3 is unreachable.
`runs/cov_0930_directed_first_tbfail.log` is the first directed run: the
lockstep matched, but the testbench's own scenario threshold for immediate
`hget` failed (1 of 5) because thread 0 drained H2T; thread 0 now delays
before `hput` and the host drains T2H when H2T is full.

Run from `test/`: `make -B COCOTB_TEST_MODULES=test_blocking` (directed
plus `BLOCK_SEEDS`, default 20, x `BLOCK_CYCLES`, default 1500, from
`BLOCK_BASE`, default 1). `BLOCK_SEEDS=0` runs only the directed cases.
The module is not in the default `COCOTB_TEST_MODULES`.

## Random lockstep and gate-level runs, 2026-09-30

Plain random lockstep on the harden commit's RTL (`c3ffca5`, `src/` unchanged since): 100 seeds x 3000 cycles, 22,539 commits matched between RTL and the Python model, pass (`runs/rand100_0930.log`, 781 s).

Gate-level simulation had never run: `test/Makefile` left out the IHP primitive library (`notes/bugs.md` 52). With that fixed, `tools/gl_run.sh <netlist> <tag> "<tests>"` runs named cocotb cases against a netlist in a scratch copy of `test/` (`GATES=yes`, `NODUMP=yes`), with the functional SRAM model. Against a yosys netlist mapped to the IHP typ library (`runs/gl/synth_netlist.v`, sha256 `06ef5ee7...`, 24,149 cell instances, pre-placement, zero-delay), all eight UART/SPI/I2C cases pass (`runs/gl_synth_0930.log`): `test_host_boot_and_uart_tx`, `test_uart_rx_fast_sender`, `test_spi_mode3_flash_read`, `test_spi_mode0_jedec`, `test_i2c_write_read_with_stretching`, `test_uc2_i2c_bringup`, `test_uc3_spi_flash_programmer`, `test_uc1_uart_autobaud_console`. This checks that synthesis preserves function; it is not the post-route netlist and carries no timing. The post-route result is recorded below when the harden completes.

## Formal witnesses and nonvacuity, 2026-09-30

All jobs: SymbiYosys, engine `smtbmc bitwuzla`, run on the PC through
`tools/pc.sh formal` with `FORMAL_TIMEOUT=900`, one at a time. Every job here
is `mode cover`: a PASS shows the named states are reachable under the job's
assumptions (a solver witness). None of these is a safety proof, and none
changes the bounds of the safety jobs in section 6. `src/` was not modified;
the core covers are included into a generated copy of `tempo_core.v`
(`formal/core_nv_gen.sh`, `formal/core_nv.vh`).

| Job | Task | Depth limit | Result | Wall | Log |
|---|---|---|---|---|---|
| `lane_miter.sby` | `cover` | 60 | PASS, both covers (step 9, step 12) | 30 s | `runs/formal_0930_miter_cover.log` |
| `lane_miter.sby` | `cover_hint` | 45 | PASS, both covers (step 38) | 169 s | `runs/formal_0930_miter_cover_hint.log` |
| `lane_anchor.sby` | `cover` | 40 | PASS, 5 of 5 covers (steps 5-7) | 4 s | `runs/formal_0930_anchor_cover.log` |
| `core_cover.sby` | `cover` | 90 | Timeout at 900 s; 5 of 5 thread covers reached (steps 36-39), plus `hp_h2t_v` at step 48 in the earlier ungrouped run | 900 s | `runs/formal_0930_core_cover.log`, `runs/formal_0930_core_cover_novcd.log` |
| `core_cover.sby` | `cover_host` | 90 | Timeout at 900 s; 2 of 4 reached, 2 unresolved | 900 s | `runs/formal_0930_core_cover_host.log` |

Witness traces: `runs/formal_0930_witness/miter_cover/` and
`.../miter_cover_hint/` (VCD and Yosys witness files).

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
  unchanged: BMC, no counterexample through step 14, timeout at step 15. It
  is not a proof, and it still covers only the constrained tape family.
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

Core invariants re-proved this session on the harden RTL: `formal/core.sby` task `prove`, k-induction depth 8, `smtbmc bitwuzla`, PASS (basecase and induction), 7 s (`runs/formal_0930_core_prove.log`). This is an unbounded proof of the asserted core invariants because the induction step starts from any state satisfying them. The depth-30 `bmc` task adds nothing about running threads (they cannot start before step 36; `notes/bugs.md` 53).

Committed-tree reruns, 2026-09-30: from a clean worktree at `fbd3292` (header line records commit and zero dirty files), RTL suite 27/27 (`runs/rtl_fbd3292.log`, 2555 s) and Python suite 193/193 (`runs/py_fbd3292.log`, 915 s). `src/`, `test/`, `tools/`, `host/` and `fw/` at later commits differ only in docs and `tools/bench_rates.py`'s head fallback. Complete-source export of `fbd3292`: 165 files; in the export, image check passes, 193/193 Python tests, three cocotb protocol cases pass, `bench_rates.py` runs without git history (`runs/export_fbd3292.log`).

Longer formal runs, 2026-09-30 (PC threads left idle by single-threaded detailed routing):

| Job | Task | Result | Time | Log |
|---|---|---|---|---|
| `core.sby` | `bmc_deep`, BMC depth 50 | PASS, no counterexample. Threads can run from step 36, so this bounds isolation/mailbox asserts over about 14 cycles of execution; bounded, not a proof. | 417 s | `runs/formal_0930_long_fcore.log` |
| `core_cover.sby` | `cover_host`, depth 90 | PASS: all 4 host covers reached, including the HGET commit and host T2H pop left unresolved earlier | 3920 s | `runs/formal_0930_long_fcov.log` (the trailing `rc=127` is from `tools/pc.sh` being edited while the job ran; SBY reports `DONE (PASS, rc=0)`) |
| `lane_miter.sby` | `bmc`, depth 60 | Timed out at 5000 s while checking step 15; no counterexample through step 14. Unchanged from 2026-09-26. | 5000 s | PC `runs/fmiter/sby.log`; local `runs/formal_0930_long_fmiter.log` has only the timeout code |

With these, every core nonvacuity cover has a solver witness. The lane timing-independence theorem remains bounded at 14 steps for the constrained tape family.
