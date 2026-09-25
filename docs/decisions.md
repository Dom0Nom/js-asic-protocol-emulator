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

Cut, in this order if more is needed: 10BASE-T becomes a simulated TX waveform only; external-clock lane mode removed (done); lanes 2-3 lite (done); data SRAM macro replaced by latches (done); CAN limited to TX, arbitration and best-effort RX; next, drop lite lanes to one.

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
