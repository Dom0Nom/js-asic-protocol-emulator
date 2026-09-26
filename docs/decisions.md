# Decisions

Newest decisions are appended at the end of each section. Each entry says what was chosen, what else was considered, and why.

## Architecture: Tempo (cut-down), 2026-09-25

Chosen: Tempo as specified in `docs/architecture.md`: a 4-thread barrel core sharing one 1024x16 SRAM, two full and two lite lanes (timed line-code engines with a symbol table), a latch scratch RAM, and a host SPI port. Runners-up: B (PRU-style RISC with timed IO) scored 56 against 59, A (PIO-plus) 51.

Why not B: smallest and most verifiable, but one thread cannot run concurrent protocols without a software scheduler, USB low speed would consume the whole core, and a public Hardcaml entry is already a PRU-style core with deadline registers and a timing analyser. B would be judged head to head against it.

Why not A: four fetches per cycle rule out a shared SRAM, program space stays at tens of instructions, and several public entries are already PIO variants.

Why Tempo: its one idea no competitor has is that pin timing is structurally independent of instruction timing, and that can be stated and proved (timing-independence theorem). The symbol table makes line codes data instead of code. The barrel makes one single-port SRAM enough for four threads.

### Critic review of the first draft and resolutions

A fresh-context reviewer attacked the first draft (27 findings: 5 critical, 19 major, 3 minor). Full text archived in `notes/critic_architecture.md`. Resolutions:

| # | Finding | Resolution |
|---|---|---|
| 1 | Novelty claims mostly prior art (XMOS, P2 smart pins, PIO divider, competitor lanes and methodology) | Accepted. Pitch rebuilt around the timing-independence theorem, the symbol table (now per-symbol length, relative levels) and capture mode; prior art credited explicitly. |
| 2 | Lanes look like fixed peripherals; threads barely touch pins | Partly accepted. Raw `start` + `emit` gives threads cycle-exact pin control on any lane pin, and PS/2 is planned as a pure-thread protocol. Rebutted the extra `iowt` instruction: `start`+`emit` already is a timed pin write, so a second mechanism would only add area. |
| 3 | Scoring omitted schedule | Accepted: schedule criterion (weight 3) added and scores revised. |
| 4 | Area under-estimated by 40-50% | Accepted: re-estimated at about 12.5k cells. Cut-down lanes and 13-bit intra-slot offsets reduce it; latches for static lane config are the next lever if synthesis runs high. |
| 5 | Two SRAM macros are the real physical risk | Accepted: one macro (IMEM 1024x16); data memory is a 32-word latch array plus `ldi` reads from IMEM for constant tables. GitHub CI and precheck before any significant RTL is a stop-and-ask item (pushing needs the user). |
| 6 | Combinational pin resolution contradicts registered outputs | Accepted: resolved pad outputs are registered (one cycle, in the spec). |
| 7 | Name the timing paths | Accepted: tracked in `notes/area.md` with each harden. |
| 8 | 16-bit time horizon | Partly accepted: per-lane prescaler (PRE) for long slots, `waitt` sets a thread LATE flag, far deadlines read as past and trip LATE. Rebutted 24-bit `NOW`: registers are 16 bits, so wider time would need multi-word arithmetic in every protocol; long timeouts loop in firmware. |
| 9 | Config writes race queued commands | Accepted: `lw`, `start` and `sync` go through the command queue, in order. |
| 10 | Firmware sketches did not assemble | Accepted: `andi` is now `simm9` (masks bytes), `xori` added, PC is 10 bits for 1024 words, sketches marked illustrative with the tested firmware in `fw/`. |
| 11 | UART RX misses back-to-back frames | Accepted: a receive command ends one cycle after its last sample. |
| 12 | Check-at-issue, commit-later races | Accepted: every instruction checks and commits in one cycle (normative). |
| 13 | RX status per lane, not per entry; overflow undefined | Accepted: per-entry `{word, count, END, ERR}`, `rx` returns END/ERR in flags, OVERRUN drops the newest word. |
| 14 | Timing failures can pass silently | Accepted: OVERRUN, thread LATE for `waitt`, lane LATE and UNDER. |
| 15 | Sticky events not consumed; write-1-to-clear race | Accepted: `wait` consumes pattern events, `stat` is read-and-clear in one commit. |
| 16 | I2C repeated START, arbitration, slave mode, glitches | Accepted: stretch applies to `emit` slots, ARB mode stops driving at the first lost bit, both patterns active in every state, per-lane input stability filter. Slave mode is firmware on patterns plus raw slots, not a lane mode. |
| 17 | Pin role semantics | Accepted: DREL, A holds its last `emit` level, pin index 15 = none, PINCONFLICT flag. EXTCLK removed (see cuts). |
| 18 | Stuffing and CRC at command boundaries | Accepted: rules written into the coding section. CAN receive declared best effort (no SJW). |
| 19 | No select or timeouts | Accepted: `wany` with a deadline; `stop` aborts a stretched lane. |
| 20 | Host DMEM starvation, MISO timing, register reads of running threads | Accepted: no host data-memory access (mailboxes instead), `HSCK <= clk/10`, register reads and PC writes only on halted threads. |
| 21 | ISA gaps | Accepted: `xori`, flag behaviour specified, `tstat`. |
| 22 | `docs/decisions.md` missing | Resolved (this file). |
| 23 | Fixed-slot codes only | Accepted: per-symbol slot length covers pulse-distance codes; capture mode covers receive of codes the pipeline cannot decode. |
| 24 | Lockstep cost and convergence | Accepted: semantics frozen here first; lockstep compares commit records and pad traces; Verilator for long runs. |
| 25 | What SBY can prove | Accepted as the formal plan (`docs/verification.md`), with width-parameterised lanes. |
| 26 | Schedule over budget | Accepted: cuts below, gates below. |
| 27 | Pick a cut-down Tempo, not A or B | Accepted. |

### Cuts and gates

Cut, in this order if more is needed: 10BASE-T becomes a simulated TX waveform only; external-clock lane mode removed (done; a receive-only external clock came back in the use-case batch below); lanes 2-3 lite (done); data SRAM macro replaced by latches (done); CAN limited to TX, arbitration and best-effort RX; drop lite lanes to one (done 2026-09-26, see below); next: latch-based lane configuration registers, then a latch register file.

Gates: SRAM harden clean and a one-lane synthesis number before the full core; UART, SPI and I2C master in RTL lockstep by the end of week 5 (2026-10-30); USB low-speed device enumerating in simulation by week 9 (2026-11-27) or USB is dropped; feature freeze in week 12 (2026-12-18).

## HDL: Verilog-2005, with ISA tables generated from Python

Chosen: plain synthesizable Verilog (the Tiny Tapeout default), with the instruction encodings emitted into `src/tempo_isa.vh` by `tools/tempo/isa.py`, the same module the assembler and simulator use.

Considered:

- Hardcaml. Jane Street uses it and it would read well, but it needs an opam/OCaml toolchain on both machines, at least one public competing entry already uses it, and the verification plan here is built on a Python golden model that can drive cocotb directly. The time it would take buys less than spending the same effort on verification depth.
- Amaranth. Python-native, so the model and the RTL would share a language, but mixing Amaranth with the foundry SRAM macros and the TT flow adds a translation layer, and cocotb plus formal on Amaranth-generated Verilog gives no advantage over hand-written Verilog.

What keeps the Verilog honest is not the language but the single source of truth: encodings live in one Python table, and every artifact that needs them is generated or checked against it (round-trip test over all 65536 encodings in `tools/tests/test_isa.py`).

## SRAM power hookup

The TT IHP template builds a single-layer Metal4 power grid (`PDN_MULTILAYER` 0), and pdngen carves its stripes around macros, so nothing reaches the SRAM's Metal4 supply columns. The urish/ttihp-sram-test recipe (macro grid with a Metal4-to-Metal4 connect) fails on the current OpenROAD with `PDN-0003 Layers must be different in connect rule`.

Chosen: a LibreLane plugin step, `Project.ExtendPowerStripes`, inserted after `OpenROAD.GeneratePDN`, which redraws the Metal4 stripes that cross the macro onto the macro's legal supply columns. The script comes from WilliamZhang20/protocol-emulator (Apache-2.0), which ported it from kdp1965/ihp-um-janestreet-prism (Apache-2.0). Local change: macro LEFs are passed from the `MACROS` config, so any SRAM size and more than one macro work without copying LEFs into the repo. Attribution is in the file headers.

Considered: writing a custom `PDN_CFG` with TopMetal1 straps (as pulp-platform/croc does). Rejected because TopMetal1 belongs to the TT top-level grid and the project routing stops at Metal4.

## Simulator timing model

The simulator is a synchronous, cycle-level model of the whole chip, not an instruction-level one: `step(pad_inputs)` returns the pad outputs driven during that cycle and then advances every register by one clock edge. Each thread's instruction is modelled as executing in one cycle (its issue cycle, `NOW mod 4 = thread`) with all effects visible from the next cycle. The RTL pipeline is arranged so its execute stage lines up with those cycles. Because the model predicts every pad on every cycle, RTL tests compare full pin traces as well as architectural state.

## Use-case RTL batch, 2026-09-25

Building the bench-tool use cases (`notes/use-case-plan.md`) and a fresh-context critic review of that plan turned up hardware needs that firmware cannot cover. They were made in one batch before the next harden, costed by synthesis of `tempo_core` against HEAD: 24,451 to 24,742 cells, 360.8k to 364.9k um² (+1.1%), 3,216 to 3,263 flops.

- `sti rd, rs`: threads store to the instruction SRAM (two slots, like `ldi`, through the thread's own fetch slot, so no other thread is delayed). Chosen over a deeper mailbox or a second RAM: the SRAM has ~800 spare words, which become receive buffers (UART console, bus monitor, edge capture) and emulated-device storage (a 24C02 image). Cost: two 26-bit latches.
- External-clock receive (`MODE.XCLK`): a receive samples I on an edge of the C input instead of on its own timeline. Needed for passive SPI and I2C monitoring and for the receive half of target emulation; the firmware alternative (pattern event plus `ior`) loses bits silently because pattern events do not queue. Receive only: a transmit on an external clock would also be needed for an SPI target and was left out (cost and verification effort; I2C target transmit works with stretching and timed slots).
- `CAP` latches only while no command runs (bug 18).
- After an `emit`, C holds the emitted level (bug 17).
- Host port on the RP2350's hardware SPI0 pins (SCK `ui[5]`, MOSI `ui[6]`, MISO `uo[3]`): the demo board for IHP shuttles carries an RP2350B (see `notes/research-host.md`), and `machine.SPI` moves bytes in C, where a Python bit-bang or PIO loop would bound every use case's throughput. Output index 11 moves to `uo[7]`. No cells.
- Mailbox commands return the count the port will take or give and never exceed it, plus an error-summary bit, and the status command reports each lane's flags (bug 19). The host never needs a status read to stay correct, and reads one only when the error bit is set.

Not taken from the critic review, with reasons: a separate IMEM read/write port for threads (the fetch-slot reuse above gives the same buffering at a fraction of the area); a channel-tagged mailbox in hardware (a firmware dispatcher does it, see the resident image); a CMSIS-DAP shim on the RP2350 (out of scope for silicon; the host library is the API, and a PC transport over the RP2350's USB serial is planned).

## Lane 3 cut for routability, 2026-09-26

The first full-design harden (commit 0e3dfa5, four lanes, 30,874 cells after placement, 54% utilization) did not route. The TT IHP flow routes signals on Metal2 to Metal4 only, so Metal3 is the single horizontal layer: global routing ended with Metal3 at 86% and 1,933 overflowed edges out of 2,081, and detailed routing went 32,154 / 16,593 / 15,902 violations over its first three iterations at about 35 minutes each. It was stopped there.

Chosen: the next step of the cut order, dropping lite lane 3. `tempo_core` synthesis: 24,742 to 20,840 cells, 364.9k to 309.7k um² (-15%). The ISA keeps the 2-bit lane field; an instruction on lane 3 is illegal (the thread halts and the host status reports it), so firmware can never silently target a missing lane. Every use case fits in three lanes: I2C moved to lane 2, the logic analyser's trigger moved to lane 0's A pin (patterns stay active in capture mode), and the three-lane concurrent cases (UART + I2C, JTAG, monitors) keep working.

Considered: lowering placement density (not possible below the actual 54% utilization), routing on Metal1 (more horizontal capacity, but pin-access and precheck risk), latch-based configuration and register file (kept as the next levers; they trade area for timing and verification effort).

## SPI throughput and rejected register-read trial, 2026-09-26

A parallel decoded-register read trial reduced local mapped core area from
20,974 cells / 310,594.32 um² to 20,887 / 304,640.63 um², with unchanged
2,767 flops. Read-port equivalence was proven and all functional tests
passed. It is **not retained**: global routing required extended clock NDR
retries, versus 5m41s for the baseline, and no higher usable clock
speed was established. Routing is the binding constraint;
a smaller synthesis number alone does not justify this tradeoff. The trial
snapshot remains in `runs/experiments/resume_decoded` on the PC. Final RTL
retains the original register muxes and the validated index-width fixes.

The SPI bulk firmware queues the next 16-bit transfer before receiving and
storing the current word, then drains the final word explicitly. It keeps the
two-half SRAM credit protocol and emits exactly 256 words per half. On the
same cycle-model board at period 6, a 1024-byte read falls from 132,889 to
125,614 cycles (5.47% fewer cycles, 5.79% more throughput). Host SPI transfer
time is included; real board throughput remains unmeasured.

SPI now exposes the lane's existing LSB-first setting without extra RTL.
Two-byte mailbox words are swapped for LSB-first transfers; odd tails keep
the firmware's high-byte convention. Modes 0/2 require period >= 5, modes
1/3 >= 6, and sample >= 4/5 respectively. Shorter settings return stale MISO
on the cycle model despite valid MOSI framing. The API rejects them before
loading firmware, and caps periods at 8191 to keep clock/sample offsets in
their 13-bit fields. These are implementation limits, not universal SPI
timing requirements; pad and target delays may raise the minima.

The submission pin map and datasheet now match `src/project.v` and the
RP2350 transport: HSCK `ui[5]`, HMOSI `ui[6]`, HMISO `uo[3]`, HCS_N `ui[7]`.
The old labels would have wired the host incorrectly. Area stays 6×4 and the
50 MHz clock remains a target pending slow-corner timing closure.

## SRAM power-domain gap, 2026-09-26

The 44 Magic extraction overlaps come from full-height Metal4 supply straps
crossing a real LEF obstruction between VDD! and VDDARRAY!. All feedback
boxes lie inside that gap, and none lies inside any declared supply pin.
After allocation and tidy, the plugin now subtracts each intersecting SRAM
Metal4 obstruction from both the straps and exported power-pin shapes,
using the LEF spacing plus 0.05 µm clearance. Existing rail vias outside
the macro feed each remaining segment. R0 and MX are handled explicitly.

The exact plugin passes a pinned-flow PDN probe with zero power-grid errors.
An independent OpenDB check finds 32 obstructed strap/pin shapes before
the fix and zero after it. The number of database shapes differs from
Magic's decomposed feedback boxes. A new full extraction/DRC/LVS run is
still required before treating the overlap count as resolved at signoff.

## Protocol startup and reprogramming, 2026-09-26

Firmware replacement now uses a 73-word temporary cleanup image before the
new image. Halting the barrel leaves autonomous lanes and their pin owners
active; `stop` preserves configuration by design. The cleanup stops all
lanes, restores writable lane registers, releases global GPIO and lane pin
ownership, clears lane flags and all eight scratch words, and drains H2T.
The host drains T2H before starting it and waits for its completion word.
It uses the existing host port and ISA, with no RTL, area or reserved SRAM
increase. The cost is extra startup transactions. `patch` remains an in-image
parameter restart, rather than a firmware replacement.

UART startup waits for an armed-receive acknowledgement, and bit timing
rounds the complete fixed-point value with a prescaler constrained by SAMP's
13 bits. This preserves the first incoming character and midpoint sampling
at low rates. Protocol tests include 300 to 2400 baud with a 2% slow sender.

I2C waits for every write-byte ACK before queuing another command. This
adds host round trips for writes, but prevents bytes or reads after a NACK.
Receive bytes still use four-word batches.

The UART-trigger tool rejects frames lasting at least 32768 core cycles.
It otherwise silently schedules from a wrapped 16-bit CAP timestamp: the
9600-baud reproduction takes 125540 cycles for a requested delay of 60000.
Its firmware also has no prescaler configuration. Extending that tool to
slow rates requires a wider software timebase and explicit prescaler
handling; a rate accepted by the UART console is not automatically valid
for this trigger tool. The restriction scales with the actual chip clock.
