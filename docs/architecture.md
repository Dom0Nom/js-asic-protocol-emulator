# Architecture

This document compares three candidate architectures, scores them, and specifies the chosen one, Tempo, precisely enough to build the simulator, assembler and RTL from it. The decision record, including the critic review of an earlier draft and how each finding was resolved, is in `docs/decisions.md`.

## Constraints that shape every candidate

Numbers measured in this repo unless marked otherwise.

- Die for 6x4 is 1289.28 x 710.64 um (916k um²). At the TT default 60% placement density about 550k um² is available for cells.
- Standard cell areas (sg13cmos5l typ lib): reset flop `dfrbpq_1` 49.0 um² (the library has neither a flop without reset nor an enable flop, so every writable register bit is a flop plus a mux2, about 67 um²), latch 30.8, mux2 18.1, nand2 7.3, xor2 14.5.
- SRAM macros (IHP, 1 port, synchronous read, 6.25 ns clock-to-data at the slow corner): 512x16 is 236.8 x 191.3 um (45k um²), 1024x16 is 236.8 x 336.5 um (80k um²). One SRAM bit costs about 10 um² against about 67 um² for a writable flop, so memory belongs in SRAM.
- The TT IHP power grid cannot reach SRAM supply pins without an extra flow step (see `docs/decisions.md`). Every macro adds physical risk, so the design uses exactly one.
- A single-port SRAM gives one access per cycle, so several state machines cannot each fetch an instruction every cycle from one shared macro.
- IO: 8 inputs, 8 outputs, 8 bidirectional pins, all through the TT mux, no synchronisers in the harness. Published IO evidence does not establish a supported CMOS5L pad rate (`docs/benchmarks.md`, IO path assumptions). USB low-speed digital PHY behavior is tested in simulation; electrical compliance and enumeration remain unverified. 10BASE-T needs 50 ns pulses and is unproven.
- Clock: 25 MHz declared (40 ns); 50 MHz is a future target (the 50 MHz routed design missed slow-corner setup by 9.19 ns). The demo-board RP2350 supplies the project clock; `Link.clock_hz` must match its actual configured frequency.
- Programs, data and control reach the chip only through those pins, so every candidate needs a host port.
- Schedule: one developer with AI assistance, 2026-09-25 to 2027-01-18, holidays included.

## Candidate A: PIO-plus (multiple small state machines)

Four RP2040-style state machines with a PIO-like 16-bit ISA, extended where PIO hurts most: a third scratch register, `add`/`cmp`, a CRC step on the input shift register, NRZI and bit-stuff options on `out`, PC-relative jumps.

- Instruction memory: four fetches per cycle cannot come from one single-port SRAM, so this needs a 4-read-port flop memory (64 x 16 = 1024 flops, about 70k um²) or four small macros (four times the PDN risk).
- Program loading: SPI slave on four pins.
- Size: about 6-7k cells.
- Firmware: UART TX is the PIO classic (`pull; set pins 0 [7]; out pins 1 [6]; jmp x-- ...`), SPI via side-set, I2C with `wait 1 pin SCL` for stretching, USB low speed only with the NRZI/stuff options (as pico-pio-usb shows).
- Weakness: timing comes from counting instructions and delays, program space is tiny, and it is derivative by construction. Several public entries already occupy this space.

## Candidate B: RISC core with timed IO (PRU-style)

One 16-bit RISC core at 50 MHz with single-cycle ALU ops, direct pin registers (PRU `R30`/`R31` style), a cycle counter and a wait-until instruction. One SRAM for instructions.

- Size: about 3.5k cells. The smallest and most verifiable candidate.
- Firmware: everything in software. USB low speed at 33 cycles per bit means NRZI, stuffing, CRC and sampling in 33 instructions per bit on the only thread.
- Weakness: one thread, so concurrent protocols need a software scheduler and any long path silently breaks timing. A public entry (Hardcaml, deadline register plus timing analyser) is already exactly this design, so it would be judged head to head against it.

## Candidate C: Tempo

Tempo separates what the firmware decides from when the pins move. Its intended timing guarantee is under verification.

1. **Four hardware threads on one barrel pipeline.** One 16-bit datapath executes four threads in strict rotation, one instruction per thread every 4 cycles (6.25 MIPS each at the declared 25 MHz). A barrel has no data hazards, no forwarding and no interlocks, and needs exactly one instruction fetch per cycle. That is what lets one single-port 1024x16 SRAM serve all threads.
2. **Lanes: timed line-code engines.** A lane owns up to four pins (data out D, data in I, clock C, aux A) and turns queued commands into pin edges on its own timeline. Transmit runs `bits -> CRC -> bit stuffing -> NRZI -> symbol -> pins`, receive runs the reverse. The **symbol table** is where the line code lives: each data bit selects symbol 0 or 1, and each symbol has its own slot length, a split point, two levels, and a flag that makes the levels relative to the current line level. NRZ (UART, SPI, I2C), Manchester and differential Manchester (10BASE-T, DALI), pulse-width codes (WS2812, 1-Wire write slots) and pulse-distance codes (NEC IR) are all register settings of the same datapath. PIO and PRU express these as instruction tricks (computed jumps into side-set tables, hand-tuned delays).
3. **Fractional slot timing.** Each lane uses an integer length plus an 8-bit fraction per slot and a power-of-two prescaler. Accumulator rounding stays within one time unit of the programmed period (one core cycle at PRE=0, `2^PRE` cycles otherwise). The programmed rate is quantized: 33 + 85/256 cycles approximates 1.5 Mbit/s at 50 MHz but advances one cycle per 768 slots relative to the ideal 33⅓-cycle period, about 39 ppm. Prescaling also lets slow codes such as IR and 300 baud fit the same registers. Clock stretching deliberately pauses the lane timeline.
4. **Timestamped inputs.** A lane's pattern matchers watch its pins (current and previous filtered sample, masked) and record the cycle of a match. A receive can hard-start on the match, so UART start bits, I2C START/STOP, USB SOP/SE0 and CAN SOF are register settings. An edge-capture mode streams timestamps of every edge into the lane's receive FIFO, which is a protocol sniffer and logic analyser for signals no line code covers.
5. **Clocked and open-drain pins are first class.** A lane drives a clock pulse inside the slot (SPI modes 0-3, JTAG, SWD, I2C), can run D and C open-drain, freezes its timeline while a stretched clock is held low, and in arbitration mode stops driving at the first lost bit (I2C multi-master, CAN).
6. **Detectable errors have sticky flags.** Late starts and `waitt` deadlines within the supported timestamp horizon, empty transmit queues with FRAME enabled, receive FIFO overruns, stuffing errors and lost arbitration are reported. These flags do not diagnose every timing or protocol failure.

**Timing-independence claim under verification.** Within a frame anchored to the same start time, with FRAME remaining enabled through its final EOF, the same initial lane state, input trace and ordered command tape, command-delivery variation should not change lane output timing while neither execution reports LATE/UNDER. The current two-copy miter checks a limited six-entry tape on a full-width lane; its proof and completion witnesses remain unfinished. It compares internal drive states and ownership, not the whole chip's pad resolver. Unanchored commands intentionally start when delivered: five extra CPU NOPs before the same `emit` shift an output edge from cycle 27 to 47 with no error flags. Identical command order alone is insufficient. Dynamic model/RTL checks exercise scheduled starts and workload variation. See `docs/verification.md` for the exact scope and evidence.

Closest prior art, and what is borrowed: barrel threads and timed, timestamped ports are XMOS XS1. NRZI and stuffing in pin hardware exist in Propeller 2 smart pins (USB mode). Fractional timing with 8 fractional bits matches the RP2040 PIO clock divider. Tempo's contributions are the symbol table as a general line-code primitive, stretch-aware and arbitration-aware clocked lanes, capture-to-FIFO, and the timing-independence property under verification (bounded so far), all within the 6x4 Tiny Tapeout allocation.

### Measured comparison with PIO and PRU

Tempo sizes are SRAM words the assembler emits for `fw/` (16-bit, parameter blocks included). PIO sizes are instructions (also 16-bit) in the `raspberrypi/pico-examples` programs.

| | Tempo | RP2040 PIO [1] | AM335x PRU-ICSS [2] |
|---|---|---|---|
| Engines | 4 barrel threads on one datapath, 6.25 MIPS each at 25 MHz; 3 lanes | 2 blocks x 4 state machines | 2 PRUs, 32-bit RISC, up to 200 MHz |
| Program store | one 1024 x 16 SRAM for all threads | 32 slots per block, shared by its 4 machines | 8 KB instruction RAM per PRU |
| Line coding in hardware | symbol table, NRZI, bit stuffing, CRC per lane | shift/side-set; coding in the program or CPU | none; in PRU software |

Firmware size per protocol, Tempo against the PIO programs in `pico-examples`. PRU is left out of this table: TI publishes no size-comparable PRU programs for these protocols, and a PRU core is a general-purpose CPU rather than a pin engine.

| Protocol | Tempo | RP2040 PIO |
|---|---|---|
| UART TX + RX | 180 words, 3 threads, 2 lanes (console bridge with 512-word ring) | `uart_tx` 4 + `uart_rx` 9 |
| SPI controller | 151 words, 1 thread, 1 lane | `spi_cpha0` 2, `spi_cpha0_cs` 9 |
| I2C controller, stretching | 124 words (host image), 64 (demo module); 1 thread, 1 lane; START/STOP in firmware | `i2c` 18 + 4-entry `set_scl_sda` table; the CPU sends START/STOP/RSTART as instruction words and handles unexpected NAK via IRQ |
| I2C target, 24C02 | 163 words, 1 thread, 3 lanes | no example |
| USB-LS packet PHY | 118 words, 2 threads, 2 lanes (NRZI, stuffing, CRC16 in the lane) | no example |
| Manchester TX | 37 words, 1 thread, 1 lane | `manchester_tx` 6 |
| UART + Manchester + I2C + compute resident together | `demo_multibus` 130 words (13% of SRAM), 4 threads, 3 lanes | 28 of one block's 32 slots for the three wire programs; START/STOP and compute on the CPU |

Per protocol, PIO programs are smaller: Tempo firmware also carries the host command protocol, framing and error handling that PIO examples leave to the Cortex-M0+. The measured difference is what one on-chip store holds without a CPU: all of `demo_multibus`, with 894 words left. Tempo is not faster; PRU runs 8 times the declared clock per core.

#### Lane versus software timing on the same core

A PRU core is a CPU that times pins in software. To measure what the lane adds over that model without a PRU, `tools/bench_swlane.py` runs the same UART TX (8N1, 16-byte stream, 25 MHz, thread 0) two ways on Tempo's cycle model: a PRU-style software loop (`fw/bench_swuart.s`: GOUT writes timed by `waitt` deadlines) and a minimal lane loop (`hget`, shift/or, `tx`, branch). The two-UART cases were also run on the RTL in lockstep with the model, with threads 1-3 busy, and reproduce the model's edge errors exactly. `tools/tests/test_bench_swlane.py` asserts the numbers.

| Metric | Software bit-bang (PRU-style) | Lane TX |
|---|---|---|
| Thread instructions per byte | 49 (integer period), 68 (fractional period at 115200) | 5 |
| Thread issue slots free during TX | 0% (spins in `waitt`) | 98.5% at 115200, 87.2% at 1 Mbaud (polling variant) |
| Worst edge error, 115200 | 3.11 cycles (124 ns, 1.43% of a bit) | 0.86 cycles (34 ns, 0.40%) |
| Worst edge error, 1 Mbaud | 3 cycles (120 ns, 12% of a bit) | 0 |
| Max rate, core/lane limit (no pad model) | 1.25 Mbaud (20 cycles per bit) | 12.5 Mbaud (2 cycles per bit; the 5-instruction feed is the limit) |
| Other three threads busy | edge times bit-identical | edge times bit-identical |
| Two UARTs on one thread, 115200 + 100k | decodes, LATE set, edges off by 31 and 56 cycles | 0 and 0 cycles, 61% of slots free |
| Two UARTs on one thread, 625k + 521k | fails to decode | 0 and 0 cycles, 53% free |

Software edges can only move on the thread's issue slot (every 4 cycles), so they land up to 3 cycles off the ideal grid unless the bit period is a multiple of 4 cycles. The barrel already isolates threads from each other (edges are identical with the other threads busy, in both methods); the lane adds isolation from the thread's own instruction stream, which is what lets one thread run two unrelated rates with zero edge error. For comparison, the pico-examples `uart_tx.pio` runs 2 state-machine instructions per data bit at 8 state-machine clocks per bit, 18 per byte, plus one FIFO write by the CPU.

This does not make Tempo faster than a PRU: at 200 MHz a PRU core issues 32 times the instructions of one Tempo thread (25 MHz / 4) and places edges on a 5 ns grid. The measured benefit is instruction cost per byte, edge accuracy that does not depend on the software schedule, and concurrency on one thread. The software loop is hand-written and not proven optimal, and no pad timing is modeled. Reproduce: `python tools/bench_swlane.py --rtl` (about 2 min; without `--rtl`, model only).

Cycle-level behavior asserted by tests (RTL suite in `test/`, 27/27 pass on 2026-09-30):

- `test_demo_resident_multibus_with_busy_host_and_memory`: consecutive UART frames start exactly 500 cycles apart, every Manchester frame matches over all 1600 cycles, and a UART frame, a Manchester payload and a 300-cycle target stretch of SCL overlap inside one I2C write, while T2's scratch counter and SRAM constant read are checked.
- `test_demo_manchester_frame_scheduled_across_counter_wrap`: a frame scheduled with `start` begins 2 cycles after its deadline at both tested phases, including a frame that straddles the 16-bit counter wrap and a deadline that wraps itself.
- `tools/tests/test_lane_uart.py::test_uart_tx_fractional_baud_has_no_drift` (cycle model): at 16.67 cycles per bit every bit period of 20 bytes stays within 0.6 cycles of ideal.

[1] RP2040 Datasheet, section 3.1-3.4 (build 2025-02-20), https://datasheets.raspberrypi.com/rp2040/rp2040-datasheet.pdf; programs from https://github.com/raspberrypi/pico-examples/tree/master/pio.
[2] TI AM335x datasheet SPRS717L, Features (PRU-ICSS), https://www.ti.com/lit/ds/symlink/am3358.pdf.

Direct bit-banging stays first class. A thread can move any lane pin with cycle-exact timing using `start` (schedule) plus `emit` (raw levels), without any line coding. Protocols with no use for the coding pipeline, such as PS/2 or custom sensor buses, are written this way.

- Program loading: host SPI port on 4 pins (`ui[7:5]`, `uo[3]`) writes the instruction SRAM, sets thread PCs, starts, stops and single-steps threads, reads registers of halted threads, and exchanges words with firmware through mailbox FIFOs while threads run.
- Size: the early 12.5k-cell estimate understated the implemented design. The current implementation has two full lanes and one lite lane; the routed 25 MHz build has 30,182 standard cells at 54% utilisation of the 6x4 tiles; the README gives its timing and signoff status. Use those measured results when evaluating fit and headroom.

## Firmware sketches

Illustrative only. The tested versions are in `fw/` and are assembled and run on the simulator in CI; where they differ, `fw/` wins.

UART TX, 8N1: 10 NRZ bits per byte, LSB first.

```
        lc   r3, 0x0200         ; stop bit (bit 9)
loop:   hget r4                 ; byte from the host mailbox
        shli r4, r4, 1          ; start bit 0 at bit 0
        or   r4, r4, r3
        tx   l0, r4, 10         ; lane shifts it out on its own timeline
        bra  loop
```

UART RX: start pattern "I was 1, I is 0", AUTOSTART, sample in the middle of the slot. A receive command ends one cycle after its last sample, allowing the lane to be rearmed during the stop bit. Back-to-back reception still depends on firmware rearming in time, sufficient host draining and supported sender timing; the RTL suite checks a 2% fast sender.

```
rx:     rxn  l1, 10             ; arm a 10-bit receive
        rx   r4, l1             ; C flag = cut short, N flag = error
        shri r4, r4, 1
        andi r4, 0xFF
        hput r4
        bra  rx
```

SPI master, modes 0-3: clock pin enabled, clock pulse `[CR, CF)` inside the slot, CPOL in `CFG`. Mode 0 uses `CR = len/2, CF = len`; mode 1 uses `CR = 0, CF = len/2`. CS is the aux pin, held by `emit` around the transfer.

```
        emit l2, r6             ; CS low (A driven 0), clock idle
        tx   l2, r4, 8, x       ; x: sample MISO at SAMP in every slot
        rx   r5, l2
        emit l2, r7             ; CS high
```

I2C master with stretching, ACK/NACK and arbitration: D (SDA) and C (SCL) open drain, `STRETCH` and `ARB` on. START, repeated START and STOP are raw `emit` slots, which also wait for a stretched SCL. Address plus ACK is one 9-bit full-duplex transfer with the ACK slot released:

```
        emit l3, r1             ; SDA low, SCL released: START
        emit l3, r2             ; SCL low
        shli r4, r4, 1          ; r4 = addr << 1 | rw, shifted up one
        ori  r4, 1              ; ACK slot released (1)
        tx   l3, r4, 9, x       ; MSB first
        rx   r5, l3             ; echo: bit 0 is ACK (0) or NACK (1)
        andi r5, 1
        bne  nack
```

With `ARB` set, a released 1 that reads back 0 before the ACK slot sets `ARBLOST` and the lane stops driving for the rest of the command.

USB low-speed device TX: D is D-, A is D+ with `DIFF`, `NRZI`, `STUFF=6`, CRC16 with the reflected polynomial and `CRCINV`. The CRC reset is a queued `lw`, so it cannot disturb the previous packet.

```
        lw   l0, crc, r1               ; r1 = 0xFFFF, applied in order
        tx   l0, r5, 16                ; r5 = PID << 8 | 0x80 (SYNC)
data:   ld   r4, [r6 + 0]
        tx   l0, r4, 8, c              ; c: include in CRC
        addi r6, 1
        djnz r7, data
        txcrc l0, 16                   ; ~CRC16, stuffed, pending stuff bit included
        emit l0, r2                    ; SE0
        emit l0, r2                    ; SE0
        emit l0, r3                    ; J, then release
```

## Scoring

Each criterion 1 (poor) to 5 (strong). These are historical architecture-selection estimates with our own weights, not Jane Street scores or measured readiness. The first draft had no schedule criterion and scored full Tempo too kindly; the critic's rescoring is retained for the decision history. Current evidence and requirements are in `docs/readiness.md`.

| Criterion (weight) | A: PIO-plus | B: RISC + timed IO | Tempo, full draft | Tempo (chosen) |
|---|---|---|---|---|
| Area fit (2) | 4 | 5 | 3 | 3 |
| Timing closure at 50 MHz (1) | 4 | 4 | 4 | 4 |
| Flexibility: new protocols after fab (3) | 3 | 3 | 5 | 5 |
| Concurrency (1) | 4 | 1 | 5 | 4 |
| Novelty (3) | 1 | 2 | 3 | 4 |
| Verifiability (2) | 4 | 5 | 2 | 3 |
| Stretch goals (1) | 3 | 1 | 3 | 3 |
| Schedule risk (3) | 4 | 5 | 2 | 3 |
| **Weighted total (max 80)** | **51** | **56** | **52** | **59** |

"Tempo (chosen)" describes the initial cut-down proposal: one SRAM macro, two full and two lite lanes, no external-clock receive, queued configuration and an intended timing-independence property. The implemented specification below has two full lanes and one lite lane, external-clock receive, and an unfinished timing proof. The changes and reasons are in `docs/decisions.md`.

## Tempo specification

This section is normative. `tools/tempo/isa.py` holds the encoding tables and `tools/tempo/sim.py` the behaviour. When this text and the model disagree, fix one of them in the same change.

### Global state

| Name | Width | Description |
|---|---|---|
| `NOW` | 16 | Cycle counter, increments every cycle, wraps. Reset 0. |
| `IMEM` | 1024 x 16 | Instruction SRAM, shared by all threads; threads can read it with `ldi`. |
| scratch | 8 x 16 | Scratch memory shared by the threads (`ld`/`st`). Constant tables live in IMEM (`ldi`), bulk data moves through the mailboxes. |
| `GOUT`, `GOE` | 16, 16 | GPIO value and enable for pins no lane drives. |
| `EV` | 8 | Event flags for thread-to-thread signalling. |
| `H2T`, `T2H` | 4 x 16 each | Mailbox FIFOs, host to threads and threads to host. |

Time comparison: `after(a, b)` is true when `(a - b) mod 2^16 < 2^15`. Firmware must schedule deadlines no more than 32767 cycles ahead. Outside this half-range horizon, comparison is ambiguous: a deadline NOW+65536+k has the same timestamp as NOW+k and may be accepted without LATE. Long waits require bounded steps or a wider firmware timebase.

### Pins

| Index | Output | Input |
|---|---|---|
| 0-7 | `uio_out[i]`, enable `uio_oe[i]` | `uio_in[i]` |
| 8-14 | `uo_out[i-8]` (always driven), except index 11, which is on `uo_out[7]` | `ui_in[i-8]` |
| 15 | none (the host MISO, on `uo_out[3]`) | none in lane pin fields; `ior` bit 15 reads `ui_in[7]` (host CS) |

The host MISO sits on `uo_out[3]` because that pad is the RP2350's SPI0 RX pin on the demo board (with SCK on `ui_in[5]` and MOSI on `ui_in[6]`), which lets the board use its hardware SPI for the host port. Output index 11 therefore appears on `uo_out[7]`; inputs are not permuted.

A lane pin field set to 15 means "no pin". Every input passes a two-flop synchroniser; lane inputs then pass an optional stability filter (`MODE.FILTER`). Output resolution: each output index takes the value and enable of the highest-priority lane that drives it (lane 0 highest), else `GOUT`/`GOE`. The resolved `uo_out`, `uio_out` and `uio_oe` are registered, so every pad changes only on a clock edge, one cycle after the lane or GPIO state that caused it. If two lanes both drive a pin, the global `PINCONFLICT` flag is set (host status).

Host port pins: `ui[7]` `HCS_N`, `ui[5]` `HSCK`, `ui[6]` `HMOSI`, `uo[3]` `HMISO`.

### Threads

Threads `T0`..`T3`. Per thread: `PC` (10 bits), `r0`..`r7` (16 bits), flags `Z`, `C`, `N`, a sticky `LATE` flag, and a run bit.

Thread `t` executes on cycles where `NOW mod 4 = t`. An instruction reads state as it is in that cycle and commits all its effects at the end of that cycle; checks and updates are one atomic step, so two threads can never both take the last queue slot, both pop one FIFO entry or both consume one event. A blocking instruction whose condition is false has no effect and is retried in the thread's next slot. `ldi` and `sti` take two slots; every other instruction takes one when it does not block.

After the host sets a thread's run bit in cycle `c`, the thread first executes in the first cycle `e >= c + 3` with `e mod 4 = t`. Reset: all threads halted, `PC = 0`.

`sti` and `ldi` use the thread's own next fetch slot for their data access (`sti` writes IMEM there instead of fetching), so both take two slots and neither delays another thread. The store data waits in one of two latches selected by thread parity: threads of equal parity execute two cycles apart, which is exactly the gap between a `sti` and its write.

### Instruction encoding

16-bit words. Register fields only ever sit at `[11:9]`, `[8:6]` or `[5:3]`, so the register file needs two read ports. `rd`, `rs`, `rt` are 3-bit register fields, `L` a 2-bit lane. `simm9` is sign extended. Branch targets are `PC + 1 + offset`. Unlisted encodings are illegal: the thread halts and the host status reports it.

| `[15:12]` | Mnemonic | Fields | Operation |
|---|---|---|---|
| 0 | ALU3 | `rd[11:9] rs[8:6] rt[5:3] fn[2:0]` | `rd = rs op rt`, `fn`: `add sub and or xor shl shr asr`. Shift count is `rt[3:0]`. Z, N from the result. C: carry for add, "no borrow" for sub, last bit shifted out for shifts with count > 0, unchanged otherwise. |
| 1 | ALU2 | `rd[11:9] rs[8:6] fn[5:0]` | `fn` 0-7: `mov not neg cmp tst par brev swab`; `fn` 8: `sti rd, rs`, `IMEM[rs mod 1024] = rd`, two slots, flags unchanged (threads can buffer data in unused SRAM; writing over code is allowed and is the programmer's problem). `0b01kkkk` `shli rd, rs, k`, `0b10kkkk` `shri`, `0b11kkkk` `rori`. `cmp` sets flags of `rd - rs` (C = no borrow), `tst` of `rd & rs`, no write. Every other ALU2 op except `mov` sets Z and N from its result; `shli`/`shri` also set C to the last bit out when k > 0. |
| 2 | `li rd, simm9` | `rd[11:9] imm[8:0]` | `rd = sext(imm)`, flags unchanged. |
| 3 | `addi rd, simm9` | same | `rd += sext(imm)`, sets Z, N, C (carry). |
| 4 | `cmpi rd, simm9` | same | flags of `rd - sext(imm)`. |
| 5 | `andi rd, simm9` | same | `rd &= sext(imm)`, sets Z, N. `andi r, 0xFF` masks a byte; `andi r, -256` clears it. |
| 6 | IMM8 | `rd[11:9] sel imm` | `[8]=0`: `lih rd, imm8`, `rd = imm8 << 8 | rd[7:0]`, flags unchanged. `[8:7]=10`: `ori rd, imm7`; `[8:7]=11`: `xori rd, imm7`, zero-extended `[6:0]`, set Z, N. |
| 7 | `ld`/`st` | `r[11:9] rs[8:6] st[5] off[4:0]` | address `(rs + off) mod 8` in scratch. `ld` writes `r`, `st` stores `r`. |
| 8 | `b cc, simm9` | `cc[11:9] off[8:0]` | `cc`: `al eq ne cs cc mi pl hi` (hi = C and not Z). |
| 9 | `djnz rd, simm9` | `rd[11:9] off[8:0]` | `rd -= 1`; branch if the result is not 0. Flags unchanged. |
| A | `j`/`call` | `link[11] 0[10] target[9:0]` | `PC = target`; `call` also writes `r7 = PC + 1`. |
| B | SYS | `ra[11:9] rb[8:6] fn[5:0]` | see SYS table |
| C | `lr`/`lw` | `L[11:10] reg[9:6] r[5:3] w[2] 00` | `w=0`: `lr r, L, reg` reads a lane register now. `w=1`: `lw L, reg, r` queues a register write (see lanes). |
| D | `ldi rd, [rs + uimm6]` | `rd[11:9] rs[8:6] off[5:0]` | `rd = IMEM[(rs + off) mod 1024]`. Two slots. |
| E | `tx`/`txcrc` | `L[11:10] n1[9:6] rs[5:3] fl[2:0]` | queue a transmit of `n1 + 1` bits of `rs`. `fl[0]` x: also sample I into the RX FIFO; `fl[1]` c: include the bits in the CRC; `fl[2]` s: send the lane CRC instead of `rs` (`txcrc`). Blocks while the queue is full. |
| F | LANE | `L[11:10] imm[9:6] r[5:3] sub[2:0]` | see LANE table |

SYS functions:

| fn | Mnemonic | Operation |
|---|---|---|
| 0 | `nop` | |
| 1 | `halt` | clear own run bit |
| 2 | `jr ra` | `PC = ra[9:0]` (`ret` = `jr r7`) |
| 3 | `now ra` | `ra = NOW` |
| 4 | `waitt ra` | block until `after(NOW, ra)`; on release, set `LATE` if `NOW - ra >= 4` |
| 5 | `ior ra` | `ra` = synchronised input pins (index order above) |
| 6 | `iow ra, rb` | `GOUT = (GOUT & ~rb) | (ra & rb)` |
| 7 | `ioe ra, rb` | `GOE = (GOE & ~rb) | (ra & rb)` |
| 8 | `hput ra` | push to `T2H`, block while full |
| 9 | `hget ra` | pop `H2T`, block while empty |
| 10 | `sev k` | set `EV[k]`, `k` in the `rb` field |
| 11 | `wev k` | block until `EV[k]`, then clear it |
| 12 | `tid ra` | `ra` = thread number |
| 13 | `hcnt ra` | `ra = (T2H free) << 8 | (H2T used)` |
| 14 | `wany ra, rb` | block until any source in mask `rb` is ready or `after(NOW, ra)`; then `ra` = ready sources masked by `rb` (0 means timeout). Sources: `[3:0]` lane k RX FIFO not empty, `[7:4]` lane k start-pattern event pending, `[8]` `H2T` not empty, `[9]` `T2H` not full, `[15:10]` `EV[0..5]`. Consumes nothing. |
| 15 | `tstat ra` | `ra` = thread sticky flags (`[0]` LATE), then clear them |

LANE sub-operations:

| sub | Mnemonic | Operation |
|---|---|---|
| 0 | `rx r, L` | pop the RX FIFO into `r`; `C` = entry cut short (END), `N` = entry error, `Z` from the word. Blocks while empty. |
| 1 | `rxn L, n[, c]` | queue a receive of `n = imm + 1` bits (1..16). Blocks while the queue is full. `c` (bit 0 of the register field) continues the current frame: the receive does not wait for the start pattern and keeps the decoder state. |
| 2 | `emit L, r` | queue one raw slot (length `LEN0`) driving the pins from `r`. Blocks while full. |
| 3 | `start L, r` | queue a start time: the next command's first slot begins at cycle `r`. Blocks while full. |
| 4 | `sync L, r` | like `start` with time `CAP + r`. |
| 5 | `wait L, c` | block until condition `imm`: 0 queue empty and lane idle, 1 start-pattern event (consumed), 2 stop-pattern event (consumed), 3 RX FIFO not empty. |
| 6 | `stop L` | abort the running command (also while stretched), flush the queue and the RX FIFO, keep pin levels. |
| 7 | `stat r, L` | `r = STAT`, then clear the sticky bits that were read as 1. |

`emit` word: bit 0 D level, bit 1 A level, bit 2 C level, bit 4 D enable, bit 5 A enable, bit 6 C enable, bit 7 end of frame. Enable 0 releases the pin. On an open-drain role, level 1 also releases.

### Lanes

Lanes 0 and 1 are full lanes. Lane 2 is lite: its NRZI, stuffing, CRC, resync and relative-symbol features are absent and read as 0. Lane 3 was cut for routability (`docs/decisions.md`): the lane field keeps 2 bits, and any instruction that names lane 3 is illegal.

Registers (`lr` reads the current value; `lw` goes through the command queue):

| # | Name | Bits | Meaning |
|---|---|---|---|
| 0 | `CFG` | 16 | mode bits, below |
| 1 | `PINS` | 16 | `[3:0]` D, `[7:4]` I, `[11:8]` C, `[15:12]` A; 15 = none |
| 2 | `LEN0` | 16 | slot length of symbol 0 (and of `emit` and receive slots), in time units |
| 3 | `LEN1` | 16 | slot length of symbol 1 |
| 4 | `SYM0` | 16 | `[15]` level B, `[14]` level A, `[13]` REL, `[12:0]` split |
| 5 | `SYM1` | 16 | same for symbol 1 |
| 6 | `FRAC` | 8 | fraction added per slot, 1/256 time unit |
| 7 | `CR` | 13 | clock pulse start in the slot |
| 8 | `CF` | 13 | clock pulse end in the slot |
| 9 | `SAMP` | 13 | sample point in the slot |
| 10 | `PAT` | 16 | start pattern `[15:8]` mask, `[7:0]` value over `{prev A,C,I,D, cur A,C,I,D}` |
| 11 | `PATE` | 16 | stop pattern, same layout |
| 12 | `CRC` | 16 | CRC state |
| 13 | `POLY` | 16 | CRC polynomial |
| 14 | `CAP` | 16 | `NOW` at the last start-pattern match (read only) |
| 15 | `MODE` | 14 | `[2:0]` PRE: one time unit is `2^PRE` cycles; `[3]` FRAME; `[4]` CRCINV; `[6:5]` RESYNC edge (0 off, 1 rising, 2 falling, 3 both); `[7]` CAPTURE; `[8]` ARB; `[11:9]` FILTER (input must be stable this many cycles); `[13:12]` XCLK: receive on an external clock (0 off, 1 rising, 2 falling, 3 both edges of the filtered C input) |

`CFG`: `[0]` MSB first, `[1]` DOD (D open drain), `[2]` COD (C open drain), `[3]` CEN, `[4]` CPOL, `[5]` STRETCH, `[6]` NRZI, `[7]` DIFF (A = not D), `[10:8]` STUFF (0 off), `[11]` STUFFANY, `[12]` AUTOSTART, `[13]` DREL (release D in receive slots and when idle instead of holding it).

`STAT`: `[0]` busy, `[1]` queue full, `[2]` RX FIFO not empty, sticky: `[3]` start match, `[4]` stop match, `[5]` LATE, `[6]` UNDER, `[7]` STUFFERR, `[8]` OVERRUN, `[9]` ARBLOST; `[15:11]` bit count (0 to 16) of the last popped RX entry.

**Queue.** Two entries, in order: `tx`, `rxn`, `emit`, `lw`, `start`/`sync`. The command in progress is not in the queue. At the end of a command the lane takes the queue head: an `lw` applies immediately and the lane looks at the next entry in the same cycle, so one `lw` between two commands costs no time; each further consecutive `lw` delays the next slot by one cycle. `start`/`sync` set the start time of the next command. With an empty queue, entries apply one per cycle as they arrive.

**Starting.** An idle lane begins the head command:

- at the pending start time, if one is set (setting `LATE` and starting at once if that time is already past);
- otherwise at the next start-pattern match if `AUTOSTART` is set and the command is a receive without `c`;
- otherwise at once.

Consecutive commands run back to back. With `FRAME` set, a command that ends while no data command is queued sets `UNDER`, unless it is an `emit` with bit 7 set, which marks the end of a frame. The timing-independence claim requires FRAME to remain enabled through this final EOF; an anchored first command alone does not diagnose later empty-queue gaps with FRAME disabled. The precise checked tape and incomplete proof are described in `docs/verification.md`.

**Slots.** A slot carrying data bit `b` lasts `LEN_b` time units, receive and `emit` slots last `LEN0`, plus one extra unit whenever the fractional accumulator (`acc += FRAC`, 8 bits) overflows. With `t` the offset in time units from slot start:

- D: for a coded bit, level A of `SYM_b` while `t < split`, level B after. With REL, the levels are XORed with the NRZI line state (differential codes). `emit` drives raw levels for its slot. In receive slots D holds (or is released with `DREL`).
- A: complement of D with `DIFF`; otherwise holds the last `emit` level.
- C (if `CEN`): `CPOL xor (CR <= t < CF)`. When the lane goes idle after a coded (`tx`/receive) slot, C returns to `CPOL`; after an `emit` it keeps the emitted level (an I2C STOP leaves SCL released). With `STRETCH` and `COD`, when C is released (at `CR`, or at the start of an `emit` slot that releases it) the lane freezes `t` until the filtered C input reads 1.
- At `t = SAMP` the lane samples I (receive commands and `tx x`). A receive command ends one cycle after its last sample, giving firmware time to rearm before a following UART start edge when sender timing permits. For normal receive completion, if a continuing receive (`c`, possibly behind one `lw`/`start`) is already queued, or the lane drives a clock (`CEN`), it runs to the end of its slot instead, keeping the continuation on the bit grid and completing the clock pulse. A stop-pattern match can terminate reception earlier, and RESYNC can hand over on an edge.
- `RESYNC` (full lanes): during a receive, an I edge of the chosen polarity sets `t` to the synchroniser plus filter latency, so the slot re-aligns to the actual edge. The fractional accumulator is not reset. There is no resynchronisation jump width limit, so CAN receive is best effort.
- `ARB`: in a `tx x` slot where D was released and the sample reads 0 (outside the last bit), set `ARBLOST` and release D for the rest of the command.
- `XCLK` (all lanes): a receive command has no timeline. It samples I on each chosen edge of the filtered C input (C is an input; `CEN` should be off) and ends after its last sample, or early on a stop-pattern match (END set). A queued continuation starts at once. This is the receive half of an SPI or I2C target and the basis of the bus monitors; the filter and synchroniser delay C and I equally. A sampling edge that arrives while no receive is running or queued sets `UNDER`: the firmware fell behind and bits were lost.

**Coding (full lanes).** Transmit, per data bit in order: CRC update if the command has `c`; stuffing (after `STUFF` equal bits, or ones unless `STUFFANY`, insert the complement without consuming data; the counter runs across commands until an `emit`; a stuff bit owed at the end of a command is sent before that command completes); NRZI (toggle the line state on 0); symbol. `txcrc` bits are stuffed but not fed into the CRC; with `CRCINV` they are inverted. The NRZI line state starts at the current D level, so an `emit` of J before a packet sets it. Receive reverses the order: NRZI decode (1 if the level did not change), destuff (drop the bit after `STUFF` equal bits; `STUFFERR` if it is not the complement), CRC, shift. An echo from `tx x` returns decoded, destuffed bits. A receive queued with `c` keeps the decoder state (NRZI reference level, destuff counter), since stuffing and NRZI span word boundaries; any other receive initialises it, to the I level before the start edge (AUTOSTART) or the current I level.

CRC step for bit `b`: LSB first, `crc = (crc >> 1) ^ (POLY if (crc ^ b) & 1)`; MSB first, `crc = (crc << 1) ^ (POLY if (crc >> 15) ^ b)`.

**Patterns.** Every cycle each lane forms `{prev A,C,I,D, cur A,C,I,D}` from the filtered inputs of its pins and compares with `PAT` and `PATE` under their masks, in every lane state. A start match sets the start-match event, and, if no command is running, `CAP = NOW`. So `CAP` holds the match that started (or could have started) the current command, e.g. a UART start bit, and data edges inside a running receive do not move it; `sync` anchors to it. A stop match sets the stop-match event and, during a receive, ends the word early: the partial word enters the RX FIFO with END set. `wait` consumes events; `stat` clears what it reads.

**RX FIFO.** Two entries of `{word, count, END, ERR}` (ERR = stuffing error in this word). Words are right-aligned: LSB-first lanes place the first bit at bit 0, MSB-first lanes place the last bit at bit 0. A word that completes while the FIFO is full is dropped and sets `OVERRUN`. `tx` without `x` produces nothing.

**Capture mode** (`MODE.CAPTURE`): the lane does not run commands; every edge of the filtered I input pushes `NOW` into the RX FIFO with ERR = the new level.

### Host port

`Link.load` first runs a temporary reset program through this same port.
It stops all three lanes, restores their writable configuration, releases
their pin ownership and global GPIO, and discards queued commands from the
previous image. Halting threads alone does not stop autonomous lanes, and
the ISA's `stop` deliberately preserves configuration and pin levels.
The reset program occupies 73 SRAM words and is overwritten by the new image;
it adds startup transactions without adding hardware or reserved SRAM space.
It also runs with `start=False`, before the user image is left halted.

UART firmware sends one zero readiness word after both receives are armed.
The host `Uart` constructor consumes it before returning; subsequent mailbox
words are received characters. UART timing uses a prescaler that fits the
midpoint in 13-bit SAMP, and carries fractional rounding into LEN.

SPI mode 0, MSB first, `HCS_N` frames a transaction, `HSCK` at most `clk / 12`. MISO moves to the next bit 3 to 5 clock cycles after each rising `HSCK` edge (synchroniser, logic, output register), so the host sees the bit it samples on a rising edge for a whole SCK period and hold time is at least 3 clock cycles. The first byte is a command; multi-byte values are big endian.

| Cmd | Name | Payload |
|---|---|---|
| `0x01` | write IMEM | addr16, then words until CS rises (auto increment) |
| `0x02` | read IMEM | addr16, one dummy byte, then words |
| `0x05` | control | byte: `[3:0]` run bits to set, `[7:4]` run bits to clear |
| `0x06` | set PC | thread byte, addr16 (halted threads only) |
| `0x07` | status | dummy, then 8 bytes: `{illegal[3:0], run[3:0]}`, `{T2H level, H2T level}`, `{PINCONFLICT, OR of lane STAT[9:3]}`, thread LATE bits, then per lane k (0-3) `{busy, STAT[9:3]}` |
| `0x08` | mailbox write | words into `H2T`. The byte after the command returns `{error, 0000, free}`: the port pushes at most `free` words (the H2T space when the command byte completed) and drops the rest, so the host knows exactly how many were taken |
| `0x09` | mailbox read | the byte after the command returns `{error, 0000, level}`; then words from `T2H`. The port pops at most `level` words; later words read as 0 and are not popped |
| `0x0A` | step | thread byte: execute one instruction of a halted thread |
| `0x0B` | read register | thread byte, register byte (0-7, 8 PC, 9 flags), dummy, word (halted threads only) |

Host IMEM accesses use cycles whose fetch slot belongs to a halted thread, so code can be loaded into a halted thread while others run.

`error` in the mailbox replies is 1 when any error flag is set: an illegal instruction, a thread `LATE`, `PINCONFLICT`, or a lane `LATE`, `UNDER`, `STUFFERR`, `OVERRUN` or `ARBLOST`. The host reads the status only when it is set, so reporting flags costs no extra transactions in the common case.

## Verification hooks built into the architecture

- The model predicts every pad on every cycle, so RTL tests compare full pin traces and architectural commits.
- The current lane RTL parameter is `FULL`; counter and slot-length widths are fixed. Formal jobs use the implemented widths with constrained short tapes, not a narrowed-width abstraction.
- LATE and UNDER make missed anchors and empty queues inside FRAME visible. They do not imply that unanchored commands are independent of delivery time; the guarantee and remaining proof work are scoped in `docs/verification.md`.
