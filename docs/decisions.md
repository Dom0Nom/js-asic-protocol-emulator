# Decisions

The design is described in [architecture.md](architecture.md).

## Barrel threads plus timed lanes

**Decision:** Four barrel threads on one 16-bit datapath, and three lanes that turn queued commands into pin edges on their own counters.

**Why:** Bit timing does not depend on instruction counts. One thread can serve several protocols, and four threads share one program memory.

**Rejected:**
- Four PIO-style state machines with an extended instruction set. Each machine fetches every cycle, which needs a multi-port flop memory or four macros. Program space stays at tens of instructions, and timing still comes from counting instructions.
- One RISC core with direct pin registers and a wait-until instruction, in the style of the TI PRU. It is the smallest option, but it has one thread, so concurrent protocols need a software scheduler, and USB low-speed coding would consume the whole core.

## One SRAM macro for all threads

**Decision:** A single 1024 x 16 single-port SRAM holds code and data. Threads read and write it with `ldi` and `sti` through their own next fetch slot. A separate 8-word flop array is the only other shared memory.

**Why:** An SRAM bit costs about a seventh of a writable flop in this library. The barrel fetches once per cycle, so one port is enough. Each macro adds power-grid and routing risk, so there is exactly one.

**Rejected:** A second SRAM for data. A separate data port for threads. A host-accessible data memory; the mailboxes replace it.

## Verilog-2005 with a generated ISA header

**Decision:** Plain Verilog-2005. Instruction encodings are defined once in [tools/tempo/isa.py](../tools/tempo/isa.py), which generates `src/tempo_isa.vh` and drives the assembler, the disassembler and the model.

**Why:** It is the Tiny Tapeout default and works directly with the foundry SRAM macro, cocotb and SymbiYosys. One encoding table removes a class of decoder mismatches; a round-trip test covers all 65536 words.

**Rejected:** Hardcaml (an OCaml toolchain on every machine, and the verification flow here is built on a Python model). Amaranth (an extra translation layer around the macro and the Tiny Tapeout flow with no verification benefit).

## Cycle-level model as the specification

**Decision:** The Python model in `tools/tempo/` predicts every pad on every cycle. The RTL is written to match it, and tests compare pad traces and instruction commits each cycle.

**Why:** The design's claims are about cycle timing; an instruction-level model cannot check them.

**Rejected:** An instruction-level simulator with separate timing tests.

## Lane configuration goes through the command queue

**Decision:** `lw`, `start` and `sync` are queue entries and apply in order between commands.

**Why:** A direct register write races the commands already queued. Queued writes let firmware change line coding or reset the CRC between two back-to-back commands at a defined point.

**Rejected:** Immediate register writes with firmware responsible for waiting until the lane is idle.

## Registered pad outputs

**Decision:** The resolved output values and enables pass one register before the pads.

**Why:** Pin resolution across three lanes and GPIO is combinational. Registering it gives glitch-free pads and a constant one-cycle delay.

**Rejected:** Combinational outputs, which would save one cycle of latency.

## Lane input register

**Decision:** Each lane registers its four selected and filtered pin values before using them.

**Why:** Pin select, filter, pattern match, sample and next-command start formed the critical path of the design.

**Cost:** One more cycle of input latency, 3 cycles in total. The edge-alignment offset is `4 + FILTER` cycles, and the SPI controller's minimum bit period is one cycle longer.

**Rejected:** Keeping the path combinational, which limits the clock.

## External-clock receive only

**Decision:** A lane can receive on an external clock edge (`MODE.XCLK`). It cannot transmit on one.

**Why:** Bus monitors and target receive need it; sampling in firmware loses bits because pattern events do not queue. An I2C target can transmit with clock stretching and timed slots.

**Rejected:** Externally clocked transmit, which an SPI target would need. It was left out for area and verification effort.

## Host port on the demo board's SPI pins

**Decision:** The host port uses `ui[5]` SCK, `ui[6]` MOSI, `uo[3]` MISO and `ui[7]` CS. Output index 11 moves to `uo[7]`.

**Why:** These are the RP2350's SPI0 pins on the Tiny Tapeout demo board, so the host uses its SPI peripheral.

**Rejected:** A contiguous pin group driven by a bit-banged host.

## SRAM power hookup

**Decision:** A LibreLane plugin step, `Project.ExtendPowerStripes` (`librelane_plugin_sram_pdn.py`), runs after PDN generation and redraws the Metal4 stripes onto the macro's supply columns. It cuts the straps at the macro's Metal4 obstructions.

**Why:** The Tiny Tapeout IHP grid is a single Metal4 layer, and pdngen carves its stripes around macros, so nothing reaches the SRAM supply pins.

**Rejected:** A macro grid with a Metal4-to-Metal4 connect rule (OpenROAD rejects it). TopMetal1 straps (that layer belongs to the Tiny Tapeout top level).

The script is adapted from WilliamZhang20/protocol-emulator and kdp1965/ihp-um-janestreet-prism (Apache-2.0).

## Per-corner SRAM timing views

**Decision:** `src/config.json` maps `nom_typ_*`, `nom_fast_*` and `nom_slow_*` to the macro's matching Liberty files.

**Why:** All IHP corner names start with `nom_`, so a `nom_*` pattern selects the typical SRAM view at every corner and hides slow-corner SRAM delay.

**Rejected:** The template's `nom_*`, `min_*`, `max_*` mapping.

## Lane 3 removed, lane 2 reduced

**Decision:** Three lanes are built. Lanes 0 and 1 are full. Lane 2 has no NRZI, stuffing, CRC, `RESYNC` or `REL`. The 2-bit lane field stays, and an instruction that names lane 3 is illegal.

**Why:** The four-lane design did not route. The flow routes signals on Metal2 to Metal4, which leaves Metal3 as the only horizontal layer, and it overflowed. Removing one reduced lane cut core cell area by about 15%. Every firmware image fits in three lanes.

**Rejected:** Lower placement density (utilisation was already below the target). Routing on Metal1 (pin-access and precheck risk). Latch-based configuration registers (timing and verification cost).

## Declared clock

**Decision:** 25 MHz, `CLOCK_PERIOD` 40 ns. <!-- CLOCK -->

**Why:** The design routed at a 20 ns target missed slow-corner setup by 9.19 ns, a minimum period of about 29.2 ns. <!-- CLOCK -->
The demo board's RP2350 produces 25 MHz exactly (150 MHz / 6). <!-- CLOCK -->
Host-library firmware computes its timing from the clock frequency, so the images do not depend on this choice.

**Rejected:** 50 MHz, which needs an RTL restructure that has not been done. <!-- CLOCK -->

## KLayout DRC as signoff, Magic DRC off

**Decision:** `RUN_MAGIC_DRC` is false. KLayout DRC, LVS and the Tiny Tapeout precheck are the signoff checks.

**Why:** Magic reports false overlaps on the SRAM macro's LEF abstract.

**Rejected:** Keeping Magic DRC as a gate, which fails on the false overlaps.
