# Architecture

Tempo is a programmable protocol emulator for a Tiny Tapeout 6x4 block on IHP SG13CMOS5L. Four barrel threads run firmware from one shared 1024 x 16 SRAM. Three lanes turn queued commands into pin edges on their own cycle counters. Threads decide what to send and what to do with received words. Lanes decide when pins move and when they are sampled. A thread that runs a few cycles early or late does not move an edge as long as the lane's queue stays fed.

Encodings are defined in [tools/tempo/isa.py](../tools/tempo/isa.py). Behaviour is defined by the cycle model ([sim.py](../tools/tempo/sim.py), [lane.py](../tools/tempo/lane.py)), which the RTL matches cycle for cycle ([verification](verification.md)).

## Blocks

- [src/project.v](../src/project.v): input synchroniser, SRAM macro, pad mapping.
- [src/tempo_core.v](../src/tempo_core.v): barrel datapath, mailboxes, GPIO, pin resolution, pad register.
- [src/tempo_lane.v](../src/tempo_lane.v): timed line-code engine, instantiated three times.
- [src/tempo_host.v](../src/tempo_host.v): SPI slave for program load, thread control, mailboxes and status.

Shared state: the 16-bit cycle counter `NOW`; the SRAM (IMEM), which threads read with `ldi` and write with `sti`; 8 words of scratch memory (`ld`, `st`); `GOUT` and `GOE` (`iow`, `ioe`) for pins no lane owns; 8 event flags `EV`; two 4-word mailbox FIFOs, `H2T` and `T2H`.

`NOW` wraps. A time `t` is in the past when `(NOW - t) mod 2^16 < 2^15`, so deadlines must lie at most 32767 cycles ahead.

### Pins

Pin indices 0-7 are `uio[7:0]`. Inputs 8-14 are `ui[6:0]`. Outputs 8-10 are `uo[2:0]`, 11 is `uo[7]` and 12-14 are `uo[6:4]`. The host port uses `ui[7]` CS, `ui[5]` SCK, `ui[6]` MOSI and `uo[3]` MISO, the RP2350 SPI0 pins of the Tiny Tapeout demo board.

A lane names up to four pins in `PINS`: data out D, data in I, clock C, auxiliary A. Index 15 means no pin. An output follows the lowest-numbered lane that owns it, otherwise `GOUT`/`GOE`. A lane owns a pin once it drives it. Two owners of one pin set the sticky `PINCONFLICT` flag. An open-drain role drives 0 or releases the pin.

### Host port

SPI mode 0, MSB first, SCK at or below clk/12. Commands: write and read IMEM (`0x01`, `0x02`), set and clear run bits (`0x05`), set a halted thread's PC (`0x06`), status (`0x07`), mailbox write and read (`0x08`, `0x09`), single-step (`0x0A`) and read a halted thread's registers (`0x0B`). Status reports thread run, illegal and `LATE` bits, mailbox levels, `PINCONFLICT` and each lane's flags.

A mailbox command first returns `{error, 0000, count}` and moves at most `count` words; `error` is the OR of all error flags. Host IMEM accesses use fetch slots of halted threads, so one thread can be reloaded while others run. Halting threads does not stop lanes; the host library runs [fw/reset.s](../fw/reset.s) before each new image.

## Threads and ISA

Thread `t` executes in cycles where `NOW mod 4 = t`. The pipeline is fetch, SRAM read, execute. A thread's next fetch starts after its previous instruction commits, so there are no hazards and no forwarding. The barrel makes one SRAM access per cycle, which lets one single-port macro serve four threads. Each thread has a 10-bit `PC`, `r0`-`r7` (16 bits), flags Z, C, N, a sticky `LATE` flag and a run bit.

An instruction reads state and commits every effect in its execute cycle, so two threads cannot take the same queue slot or FIFO entry. A blocking instruction whose condition is false changes nothing and is retried in the thread's next slot. `ldi` and `sti` take two slots; the second slot's fetch is the data access. An illegal encoding, including any lane instruction on lane 3, halts the thread and sets its illegal flag.

| Op | Instructions | Notes |
|---|---|---|
| 0 | `add sub and or xor shl shr asr rd, rs, rt` | |
| 1 | `mov not neg cmp tst par brev swab rd, rs`; `shli shri rori rd, rs, k`; `sti rd, rs` | `sti`: `IMEM[rs] = rd` |
| 2-6 | `li addi cmpi andi rd, simm9`; `lih rd, imm8`; `ori xori rd, imm7` | |
| 7, D | `ld st r, [rs + off]`; `ldi rd, [rs + uimm6]` | scratch; IMEM |
| 8-A | `b cc, simm9`; `djnz rd, simm9`; `j`, `call target` | `call` links in `r7` |
| B | `nop halt jr now waitt ior iow ioe hput hget sev wev tid hcnt wany tstat` | system |
| C | `lr r, L, reg`; `lw L, reg, r` | read a lane register; queue a write |
| E | `tx L, rs, n[, x][, c]`; `txcrc L, n` | queue a transmit of 1-16 bits |
| F | `rx rxn emit start sync wait stop stat` | lane operations |

- `waitt ra` blocks until `NOW` reaches `ra` and sets the thread's `LATE` flag if released 4 or more cycles after it.
- `hput` blocks while `T2H` is full, `hget` while `H2T` is empty, `wev k` until `sev k` sets `EV[k]`.
- `wany ra, rb` blocks until a source in mask `rb` is ready or `NOW` reaches `ra`. Sources: lane RX words, lane start events, mailbox levels, `EV[5:0]`.
- `tx`, `txcrc`, `rxn`, `emit`, `start`, `sync` and `lw` push one queue entry and block while the queue is full. `tx` flag `x` also samples I; `c` includes the bits in the CRC.
- `rxn L, n[, c]` queues a receive of 1-16 bits. `c` continues a frame and keeps the decoder state.
- `emit L, r` queues one raw slot: `r[2:0]` are the C, A, D levels, `r[6:4]` their enables, `r[7]` marks end of frame.
- `start L, r` queues a start time for the next command; `sync L, r` uses `CAP + r`.
- `rx r, L` pops a received word and blocks while the FIFO is empty. C: ended early. N: stuffing error.
- `wait L, c` blocks until the lane is idle (0), a start or stop event (1, 2; consumed), or an RX word (3).
- `stop L` aborts the command and flushes queue and RX FIFO. `stat r, L` reads `STAT` and clears its sticky bits.

## Lanes

A lane has a two-entry command queue, a running command, a two-entry RX FIFO and 16 registers. Lane 2 is reduced (`FULL = 0`): it has no NRZI, bit stuffing, CRC, `RESYNC` or `REL`, and those bits read 0.

### Commands and slots

Commands run in queue order. Register writes go through the queue, so they apply between commands; one `lw` between two commands costs no time.

A transmit slot for coded bit `b` lasts `LEN_b` time units; receive and `emit` slots last `LEN0`. One unit is `2^PRE` cycles. Each slot adds `FRAC` to an 8-bit accumulator, and a carry lengthens that slot by one unit. The mean slot is `LEN + FRAC/256` units, and every boundary is within one unit of its ideal position.

Within a slot, with `t` the offset in units:

- D drives level A of `SYM_b` while `t < split`, then level B. Equal levels give NRZ; opposite levels split at half the slot give Manchester ([fw/manchester_core.inc](../fw/manchester_core.inc)). With `REL` both levels are XORed with the D level at the end of the previous slot.
- C, with `CEN`, is `CPOL xor (CR <= t < CF)`: SPI modes 0-3, JTAG, SWD and I2C clocks.
- I is sampled at `t = SAMP` in receive slots and in `tx ..., x` slots.
- A is the complement of D with `DIFF`; otherwise it holds its last `emit` level.

A receive ends at its last sample, or at the end of that slot when the lane drives the clock or a continuing receive is queued.

An idle lane starts the head command at the queued start time if one is pending; otherwise, for a receive without `c` under `AUTOSTART`, at the next start-pattern match; otherwise at once.

### Patterns and filter

Every cycle the lane compares `{prev A, C, I, D, cur A, C, I, D}` with `PAT` and `PATE`. A `PAT` match sets the start event and, with no command running, `CAP = NOW`. A `PATE` match sets the stop event and ends a running receive early. A UART start bit, I2C START and STOP, and USB SE0 are pattern settings. `FILTER = n` (1-7) makes the lane ignore an input change shorter than `n` cycles.

### Line coding (lanes 0 and 1)

Transmit order per bit: CRC update (commands with `c`), bit stuffing, NRZI, symbol. Stuffing inserts the complement after `STUFF` equal ones, or equal bits of either value with `STUFFANY`; the run count carries across commands. NRZI toggles the line on a 0. Receive reverses the order and sets `STUFFERR` on a wrong stuff bit. The CRC is 16 bits with a programmable polynomial; `txcrc` sends it, inverted with `CRCINV`.

### Receive options

- `RESYNC` (lanes 0 and 1): an I edge of the selected polarity during a receive realigns the slot timer to that edge.
- `XCLK`: a receive has no timeline and samples I on the selected edge of the C input ([fw/spimon.s](../fw/spimon.s), [fw/i2ctarget.s](../fw/i2ctarget.s)).
- `CAPTURE`: commands do not run. Each I edge pushes `NOW` into the RX FIFO ([fw/la.s](../fw/la.s)).

### Clock stretching and arbitration

With `STRETCH` and `COD`, the lane freezes its slot timer where it releases C until the C input reads 1. With `ARB`, a `tx ..., x` slot in which D is released and I samples 0 sets `ARBLOST`, and D stays released for the rest of the command. The last bit is exempt, so an I2C ACK does not trigger it.

### Registers

| # | Name | Fields |
|---|---|---|
| 0 | `CFG` | `[0]` MSB first, `[1]` DOD, `[2]` COD (open drain), `[3]` CEN, `[4]` CPOL, `[5]` STRETCH, `[6]` NRZI, `[7]` DIFF, `[10:8]` STUFF, `[11]` STUFFANY, `[12]` AUTOSTART, `[13]` DREL (release D when not sending) |
| 1 | `PINS` | `[3:0]` D, `[7:4]` I, `[11:8]` C, `[15:12]` A |
| 2-3 | `LEN0`, `LEN1` | slot length of symbol 0 and 1 |
| 4-5 | `SYM0`, `SYM1` | `[15]` level B, `[14]` level A, `[13]` REL, `[12:0]` split |
| 6 | `FRAC` | 8 bits, 1/256 unit per slot |
| 7-9 | `CR`, `CF`, `SAMP` | 13 bits: clock pulse start, end, sample point |
| 10-11 | `PAT`, `PATE` | `[15:8]` mask, `[7:0]` value |
| 12-13 | `CRC`, `POLY` | CRC state and polynomial |
| 14 | `CAP` | start-match time, read only |
| 15 | `MODE` | `[2:0]` PRE, `[3]` FRAME, `[4]` CRCINV, `[6:5]` RESYNC, `[7]` CAPTURE, `[8]` ARB, `[11:9]` FILTER, `[13:12]` XCLK. Edge selects: 1 rising, 2 falling, 3 both |

`STAT`: `[0]` busy, `[1]` queue full, `[2]` RX word available; sticky `[3]` start match, `[4]` stop match, `[5]` LATE, `[6]` UNDER, `[7]` STUFFERR, `[8]` OVERRUN, `[9]` ARBLOST; `[15:11]` bit count of the last popped word.

## Timing model

Exact, in clock cycles:

- Inputs reach a lane 3 cycles after the pad: two synchroniser flops and one lane input register. `FILTER = n` adds `n` cycles.
- Outputs pass one pad register, so pads change one cycle after the lane or GPIO state that causes them.
- A command with a queued start time `T` begins its first slot in cycle `T`. The test board sees the pad edge at `T + 2` ([test/test_usecases.py](../test/test_usecases.py)).
- A command queued before the previous one ends follows it with no gap.
- On an `AUTOSTART` or `RESYNC` edge the slot timer is loaded with `4 + FILTER` cycles, so `SAMP`, `CR` and `CF` count from the pad edge.

Not exact:

- A command without a start time, queued to an idle lane, starts when it arrives.
- GPIO written with `iow` moves only in the thread's slot, every fourth cycle.
- An asynchronous input edge has one cycle of sampling uncertainty.

A late thread sets a sticky flag:

- `LATE`: the start time had passed when the command could begin. The command starts at once.
- `UNDER`: with `FRAME`, a command ended with no data command queued and was not an `emit` with bit 7. Also an `XCLK` edge with no receive running or queued.
- `OVERRUN`: an RX word completed with the FIFO full and was dropped.

Intended property: for a frame anchored with `start` and run with `FRAME`, lane output timing does not depend on when the thread delivered the commands unless `LATE` or `UNDER` is set. A bounded miter ([formal/lane_miter.sv](../formal/lane_miter.sv)) checks it. It is not proven ([verification](verification.md)).

## Measured comparison with PIO and PRU

| | Tempo | RP2040 PIO [1] | AM335x PRU [2] |
|---|---|---|---|
| Engines | 4 threads at clk/4 each; 3 lanes | 2 blocks of 4 state machines | 2 cores, 200 MHz |
| Program store | 1024 words, shared | 32 instructions per block | 8 KB per core |
| Bit timing | lane slot counter | instruction count, clock divider | instruction count |
| Line coding | lane symbols; NRZI, stuffing, CRC on two lanes | in the program or on the CPU | in software |

Tempo is slower than both. Its program store is larger than a PIO block's: [fw/demo_multibus.s](../fw/demo_multibus.s) runs UART TX, Manchester TX, an I2C controller with clock stretching and a memory test in 130 words.

No PIO or PRU hardware was measured. The table compares two ways of sending one 8N1 UART stream from a Tempo thread: a software loop that times `iow` writes with `waitt`, as a CPU with fast GPIO does, and a loop that feeds a lane ([fw/bench_swuart.s](../fw/bench_swuart.s)). It runs on the cycle model, the two-UART cases also on the RTL, without pad delays (`tools/tests/test_bench_swlane.py`).

| Measurement | Software loop | Lane |
|---|---|---|
| Instructions per byte, 25 cycles per bit | 49 | 5 |
| Thread slots left free, 25 cycles per bit | 0 | over 87% |
| Worst edge error, 25 cycles per bit | 3 cycles | 0 |
| Worst edge error, 115200 baud at 25 MHz (217.01 cycles per bit) | 3.1 cycles | under 1 cycle <!-- CLOCK --> |
| Shortest working bit | 20 cycles | 2 cycles |
| Two UARTs, one thread, 217 and 250 cycles per bit | decodes; `LATE`; edges off by 31 and 56 cycles | edge error 0 and 0 |
| Two UARTs, one thread, 40 and 48 cycles per bit | does not decode | within 2% of a bit |

Software edges move only in the thread's slot, so they sit up to 3 cycles off unless the bit period is a multiple of 4. Load on the other threads moves no edge in either method. The lane also removes the dependence on the thread's own instruction stream.

[1] RP2040 Datasheet, chapter 3, https://datasheets.raspberrypi.com/rp2040/rp2040-datasheet.pdf
[2] TI AM335x datasheet SPRS717, https://www.ti.com/lit/ds/symlink/am3358.pdf

## Limits

- All results come from simulation, formal runs and static timing. No simulation includes pad delay.
- 10BASE-T is not supported. [fw/manchester.s](../fw/manchester.s) shows Manchester coding at 50 cycles per bit, not an Ethernet PHY.
- USB low speed is packet level only ([fw/usb_ls_phy.s](../fw/usb_ls_phy.s)): SYNC, NRZI, stuffing, CRC16 and EOP, in simulation. There is no enumeration, device stack or electrical compliance.
- A USB low-speed bit is 16.67 cycles at 25 MHz. <!-- CLOCK -->
- Lane 2 has no NRZI, stuffing, CRC, `RESYNC` or `REL`. There is no lane 3.
- The host library's SPI controller needs at least 6 cycles per bit in modes 0 and 2, and 7 in modes 1 and 3.
- There is no externally clocked transmit, so no SPI target.
- Queues and RX FIFOs hold two entries.
- Timestamps and the CRC are 16 bits. `RESYNC` has no jump-width limit.

## Appendix: protocol reference

I2C, NXP UM10204 Rev. 7.0, Table 11:

| Minimum | Standard | Fast |
|---|---|---|
| tLOW, tBUF | 4.7 us | 1.3 us |
| tHIGH, tHD;STA, tSU;STO | 4.0 us | 0.6 us |
| tSU;STA | 4.7 us | 0.6 us |
| tSU;DAT | 250 ns | 100 ns |

JTAG (TI SSYA002C) and SWD (Arm IHI 0031G):

- TMS and TDI are sampled on rising TCK; TDO changes on falling TCK. Five TCKs with TMS high reach Test-Logic-Reset (p. 3-5, 3-6).
- SWD line reset: at least 50 clocks with SWDIO high, then two idle clocks (B4.3.3). JTAG-to-SWD: `0xE79E` LSB first (B5.2.2).

USB low speed, USB 2.0 specification, chapter 7:

- 1.5 Mbit/s with 1.5% tolerance (7.1.11). Idle is J: D- high, D+ low, device pull-up on D- (7.1.5.1, 7.1.7).
- NRZI: a 0 toggles, a 1 holds (7.1.8). A 0 is stuffed after six consecutive 1s, before NRZI (7.1.9).
- SYNC is KJKJKJKK (7.1.10). EOP is SE0 for two bit times, then J for one (7.1.13.2).
