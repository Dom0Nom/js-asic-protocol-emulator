# Architecture

This document compares three candidate architectures, scores them, and specifies the chosen one, Tempo, precisely enough to build the simulator, assembler and RTL from it. Background and citations are in `docs/research.md`. The decision record, including the critic review of an earlier draft and how each finding was resolved, is in `docs/decisions.md`.

## Constraints that shape every candidate

Numbers measured in this repo unless marked otherwise.

- Die for 6x4 is 1289.28 x 710.64 um (916k um²). At the TT default 60% placement density about 550k um² is available for cells.
- Standard cell areas (sg13cmos5l typ lib): reset flop `dfrbpq_1` 49.0 um² (the library has neither a flop without reset nor an enable flop, so every writable register bit is a flop plus a mux2, about 67 um²), latch 30.8, mux2 18.1, nand2 7.3, xor2 14.5.
- SRAM macros (IHP, 1 port, synchronous read, 6.25 ns clock-to-data at the slow corner): 512x16 is 236.8 x 191.3 um (45k um²), 1024x16 is 236.8 x 336.5 um (80k um²). One SRAM bit costs about 10 um² against about 67 um² for a writable flop, so memory belongs in SRAM.
- The TT IHP power grid cannot reach SRAM supply pins without an extra flow step (see `docs/decisions.md`). Every macro adds physical risk, so the design uses exactly one.
- A single-port SRAM gives one access per cycle, so several state machines cannot each fetch an instruction every cycle from one shared macro.
- IO: 8 inputs, 8 outputs, 8 bidirectional pins, all through the TT mux, no synchronisers in the harness. No official IHP pad speed exists: sky130 is rated 33 MHz out and 66 MHz in, and the only IHP silicon data point is a clean 15 MHz ring-oscillator output. USB low speed (1.5 Mbit/s) is safe. 10BASE-T needs 50 ns pulses and is unproven.
- Clock: 50 MHz target. The demo board RP2040 generates the project clock and can produce other frequencies (40, 48, 60 MHz).
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

Tempo separates what the firmware decides from when the pins move, and makes that separation a provable property.

1. **Four hardware threads on one barrel pipeline.** One 16-bit datapath executes four threads in strict rotation, one instruction per thread every 4 cycles (12.5 MIPS each at 50 MHz). A barrel has no data hazards, no forwarding and no interlocks, and needs exactly one instruction fetch per cycle. That is what lets one single-port 1024x16 SRAM serve all threads.
2. **Lanes: timed line-code engines.** A lane owns up to four pins (data out D, data in I, clock C, aux A) and turns queued commands into pin edges on its own timeline. Transmit runs `bits -> CRC -> bit stuffing -> NRZI -> symbol -> pins`, receive runs the reverse. The **symbol table** is where the line code lives: each data bit selects symbol 0 or 1, and each symbol has its own slot length, a split point, two levels, and a flag that makes the levels relative to the current line level. NRZ (UART, SPI, I2C), Manchester and differential Manchester (10BASE-T, DALI), pulse-width codes (WS2812, 1-Wire write slots) and pulse-distance codes (NEC IR) are all register settings of the same datapath. PIO and PRU express these as instruction tricks (computed jumps into side-set tables, hand-tuned delays).
3. **Exact, fractional, stall-proof time.** Each lane's slots come from a phase accumulator (integer length plus an 8-bit fraction per slot) with a power-of-two prescaler, so 1.5 Mbit/s at 50 MHz (33.33 cycles) has at most one cycle of jitter and no drift, and slow codes (IR, 300 baud) fit the same registers.
4. **Timestamped inputs.** A lane's pattern matchers watch its pins (current and previous filtered sample, masked) and record the cycle of a match. A receive can hard-start on the match, so UART start bits, I2C START/STOP, USB SOP/SE0 and CAN SOF are register settings. An edge-capture mode streams timestamps of every edge into the lane's receive FIFO, which is a protocol sniffer and logic analyser for signals no line code covers.
5. **Clocked and open-drain pins are first class.** A lane drives a clock pulse inside the slot (SPI modes 0-3, JTAG, SWD, I2C), can run D and C open-drain, freezes its timeline while a stretched clock is held low, and in arbitration mode stops driving at the first lost bit (I2C multi-master, CAN).
6. **Nothing fails silently.** Missed start times, transmit underruns mid-frame, receive FIFO overruns, late `waitt`, stuffing errors and lost arbitration all set sticky flags.

**Timing-independence theorem.** For two runs in which every lane receives the same command stream and no timing flag fires, the pin traces are identical, whatever the thread interleaving, stalls, host traffic or instruction timing. Designs whose timing comes from counting instructions (PIO, PRU, and deadline-register cores) cannot state this. It is proved formally on a reduced-width lane with a two-copy miter in SymbiYosys, and checked dynamically by random stall injection in both the model and the RTL. See `docs/verification.md`.

Closest prior art, and what is borrowed: barrel threads and timed, timestamped ports are XMOS XS1. NRZI and stuffing in pin hardware exist in Propeller 2 smart pins (USB mode). Fractional timing with 8 fractional bits matches the RP2040 PIO clock divider. Tempo's contributions are the symbol table as a general line-code primitive, stretch-aware and arbitration-aware clocked lanes, capture-to-FIFO, and the timing-independence property with its proof, all in 0.9 mm².

Direct bit-banging stays first class. A thread can move any lane pin with cycle-exact timing using `start` (schedule) plus `emit` (raw levels), without any line coding. Protocols with no use for the coding pipeline, such as PS/2 or custom sensor buses, are written this way.

- Program loading: host SPI port on 4 pins (`ui[7:5]`, `uo[7]`) writes the instruction SRAM, sets thread PCs, starts, stops and single-steps threads, reads registers of halted threads, and exchanges words with firmware through mailbox FIFOs while threads run.
- Size (re-estimated after the review): barrel core with 32 x 16-bit registers and an 8-word scratch memory about 4k cells, full lanes about 1.8k cells each, lite lanes (no NRZI, stuffing, CRC, resync) about 1.3k each, host port 0.8k, registered pin resolution 0.8k: about 12.5k cells, roughly 250k um², plus the 80k um² SRAM. About 60% of the usable area.

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

UART RX: start pattern "I was 1, I is 0", AUTOSTART, sample in the middle of the slot. A receive command ends at its last sample, so the lane is idle again in the middle of the stop bit and never misses a start bit from a slightly fast sender.

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

Each criterion 1 (poor) to 5 (strong). Weights reflect what the judges value and what one developer can finish. The first draft of this table had no schedule criterion and scored full Tempo too kindly; the critic's rescoring is adopted.

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

"Tempo (chosen)" is the cut-down design specified below: one SRAM macro, two full and two lite lanes, no external-clock lane mode, queued configuration, and the timing-independence property as the headline verification result.

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

Time comparison: `after(a, b)` is true when `(a - b) mod 2^16 < 2^15`. Deadlines further than 32767 cycles away read as already passed; the flags below make that visible instead of silent.

### Pins

| Index | Output | Input |
|---|---|---|
| 0-7 | `uio_out[i]`, enable `uio_oe[i]` | `uio_in[i]` |
| 8-14 | `uo_out[i-8]` (always driven) | `ui_in[i-8]` |
| 15 | none (`uo_out[7]` is the host MISO) | none in lane pin fields; `ior` bit 15 reads `ui_in[7]` (host CS) |

A lane pin field set to 15 means "no pin". Every input passes a two-flop synchroniser; lane inputs then pass an optional stability filter (`MODE.FILTER`). Output resolution: each output index takes the value and enable of the highest-priority lane that drives it (lane 0 highest), else `GOUT`/`GOE`. The resolved `uo_out`, `uio_out` and `uio_oe` are registered, so every pad changes only on a clock edge, one cycle after the lane or GPIO state that caused it. If two lanes both drive a pin, the global `PINCONFLICT` flag is set (host status).

Host port pins: `ui[7]` `HCS_N`, `ui[6]` `HSCK`, `ui[5]` `HMOSI`, `uo[7]` `HMISO`.

### Threads

Threads `T0`..`T3`. Per thread: `PC` (10 bits), `r0`..`r7` (16 bits), flags `Z`, `C`, `N`, a sticky `LATE` flag, and a run bit.

Thread `t` executes on cycles where `NOW mod 4 = t`. An instruction reads state as it is in that cycle and commits all its effects at the end of that cycle; checks and updates are one atomic step, so two threads can never both take the last queue slot, both pop one FIFO entry or both consume one event. A blocking instruction whose condition is false has no effect and is retried in the thread's next slot. `ldi` takes two slots; every other instruction takes one when it does not block.

After the host sets a thread's run bit in cycle `c`, the thread first executes in the first cycle `e >= c + 3` with `e mod 4 = t`. Reset: all threads halted, `PC = 0`.

### Instruction encoding

16-bit words. Register fields only ever sit at `[11:9]`, `[8:6]` or `[5:3]`, so the register file needs two read ports. `rd`, `rs`, `rt` are 3-bit register fields, `L` a 2-bit lane. `simm9` is sign extended. Branch targets are `PC + 1 + offset`. Unlisted encodings are illegal: the thread halts and the host status reports it.

| `[15:12]` | Mnemonic | Fields | Operation |
|---|---|---|---|
| 0 | ALU3 | `rd[11:9] rs[8:6] rt[5:3] fn[2:0]` | `rd = rs op rt`, `fn`: `add sub and or xor shl shr asr`. Shift count is `rt[3:0]`. Z, N from the result. C: carry for add, "no borrow" for sub, last bit shifted out for shifts with count > 0, unchanged otherwise. |
| 1 | ALU2 | `rd[11:9] rs[8:6] fn[5:0]` | `fn` 0-7: `mov not neg cmp tst par brev swab`. `0b01kkkk` `shli rd, rs, k`, `0b10kkkk` `shri`, `0b11kkkk` `rori`. `cmp` sets flags of `rd - rs` (C = no borrow), `tst` of `rd & rs`, no write. Every other ALU2 op except `mov` sets Z and N from its result; `shli`/`shri` also set C to the last bit out when k > 0. |
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

Lanes 0 and 1 are full lanes. Lanes 2 and 3 are lite: their NRZI, stuffing, CRC, resync and relative-symbol features are absent and read as 0.

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
| 15 | `MODE` | 16 | `[2:0]` PRE: one time unit is `2^PRE` cycles; `[3]` FRAME; `[4]` CRCINV; `[6:5]` RESYNC edge (0 off, 1 rising, 2 falling, 3 both); `[7]` CAPTURE; `[8]` ARB; `[11:9]` FILTER (input must be stable this many cycles) |

`CFG`: `[0]` MSB first, `[1]` DOD (D open drain), `[2]` COD (C open drain), `[3]` CEN, `[4]` CPOL, `[5]` STRETCH, `[6]` NRZI, `[7]` DIFF (A = not D), `[10:8]` STUFF (0 off), `[11]` STUFFANY, `[12]` AUTOSTART, `[13]` DREL (release D in receive slots and when idle instead of holding it).

`STAT`: `[0]` busy, `[1]` queue full, `[2]` RX FIFO not empty, sticky: `[3]` start match, `[4]` stop match, `[5]` LATE, `[6]` UNDER, `[7]` STUFFERR, `[8]` OVERRUN, `[9]` ARBLOST; `[15:12]` bit count minus 1 of the last popped RX entry.

**Queue.** Two entries, in order: `tx`, `rxn`, `emit`, `lw`, `start`/`sync`. The command in progress is not in the queue. At the end of a command the lane takes the queue head: an `lw` applies immediately and the lane looks at the next entry in the same cycle, so one `lw` between two commands costs no time; each further consecutive `lw` delays the next slot by one cycle. `start`/`sync` set the start time of the next command. With an empty queue, entries apply one per cycle as they arrive.

**Starting.** An idle lane begins the head command:

- at the pending start time, if one is set (setting `LATE` and starting at once if that time is already past);
- otherwise at the next start-pattern match if `AUTOSTART` is set and the command is a receive without `c`;
- otherwise at once.

Consecutive commands run back to back. With `FRAME` set, a command that ends while no data command is queued sets `UNDER`, unless it is an `emit` with bit 7 set, which marks the end of a frame. So inside a frame every gap in the command stream is either anchored by `start`/`sync` or flagged, which is what the timing-independence theorem needs.

**Slots.** A slot carrying data bit `b` lasts `LEN_b` time units, receive and `emit` slots last `LEN0`, plus one extra unit whenever the fractional accumulator (`acc += FRAC`, 8 bits) overflows. With `t` the offset in time units from slot start:

- D: for a coded bit, level A of `SYM_b` while `t < split`, level B after. With REL, the levels are XORed with the NRZI line state (differential codes). `emit` drives raw levels for its slot. In receive slots D holds (or is released with `DREL`).
- A: complement of D with `DIFF`; otherwise holds the last `emit` level.
- C (if `CEN`): `CPOL xor (CR <= t < CF)`. With `STRETCH` and `COD`, when C is released (at `CR`, or at the start of an `emit` slot that releases it) the lane freezes `t` until the filtered C input reads 1.
- At `t = SAMP` the lane samples I (receive commands and `tx x`). A receive command ends one cycle after its last sample, so a new start edge (a UART start bit from a slightly fast sender) is never missed; but if a continuing receive (`c`, possibly behind one `lw`/`start`) is already queued, it runs to the end of its slot instead, so the continuation stays on the bit grid.
- `RESYNC` (full lanes): during a receive, an I edge of the chosen polarity sets `t` to the synchroniser plus filter latency, so the slot re-aligns to the actual edge. The fractional accumulator is not reset. There is no resynchronisation jump width limit, so CAN receive is best effort.
- `ARB`: in a `tx x` slot where D was released and the sample reads 0 (outside the last bit), set `ARBLOST` and release D for the rest of the command.

**Coding (full lanes).** Transmit, per data bit in order: CRC update if the command has `c`; stuffing (after `STUFF` equal bits, or ones unless `STUFFANY`, insert the complement without consuming data; the counter runs across commands until an `emit`; a stuff bit owed at the end of a command is sent before that command completes); NRZI (toggle the line state on 0); symbol. `txcrc` bits are stuffed but not fed into the CRC; with `CRCINV` they are inverted. The NRZI line state starts at the current D level, so an `emit` of J before a packet sets it. Receive reverses the order: NRZI decode (1 if the level did not change), destuff (drop the bit after `STUFF` equal bits; `STUFFERR` if it is not the complement), CRC, shift. An echo from `tx x` returns decoded, destuffed bits. A receive queued with `c` keeps the decoder state (NRZI reference level, destuff counter), since stuffing and NRZI span word boundaries; any other receive initialises it, to the I level before the start edge (AUTOSTART) or the current I level.

CRC step for bit `b`: LSB first, `crc = (crc >> 1) ^ (POLY if (crc ^ b) & 1)`; MSB first, `crc = (crc << 1) ^ (POLY if (crc >> 15) ^ b)`.

**Patterns.** Every cycle each lane forms `{prev A,C,I,D, cur A,C,I,D}` from the filtered inputs of its pins and compares with `PAT` and `PATE` under their masks, in every lane state. A start match sets the start-match event and `CAP = NOW`. A stop match sets the stop-match event and, during a receive, ends the word early: the partial word enters the RX FIFO with END set. `wait` consumes events; `stat` clears what it reads.

**RX FIFO.** Two entries of `{word, count, END, ERR}` (ERR = stuffing error in this word). Words are right-aligned: LSB-first lanes place the first bit at bit 0, MSB-first lanes place the last bit at bit 0. A word that completes while the FIFO is full is dropped and sets `OVERRUN`. `tx` without `x` produces nothing.

**Capture mode** (`MODE.CAPTURE`): the lane does not run commands; every edge of the filtered I input pushes `NOW` into the RX FIFO with ERR = the new level.

### Host port

SPI mode 0, MSB first, `HCS_N` frames a transaction, `HSCK` at most `clk / 12`. MISO moves to the next bit 3 to 5 clock cycles after each rising `HSCK` edge (synchroniser, logic, output register), so the host sees the bit it samples on a rising edge for a whole SCK period and hold time is at least 3 clock cycles. The first byte is a command; multi-byte values are big endian.

| Cmd | Name | Payload |
|---|---|---|
| `0x01` | write IMEM | addr16, then words until CS rises (auto increment) |
| `0x02` | read IMEM | addr16, one dummy byte, then words |
| `0x05` | control | byte: `[3:0]` run bits to set, `[7:4]` run bits to clear |
| `0x06` | set PC | thread byte, addr16 (halted threads only) |
| `0x07` | status | dummy, then: run bits, illegal-instruction bits, mailbox levels, OR of all lane sticky flags plus `PINCONFLICT` |
| `0x08` | mailbox write | words into `H2T` (dropped when full) |
| `0x09` | mailbox read | dummy, then words from `T2H` (0 when empty) |
| `0x0A` | step | thread byte: execute one instruction of a halted thread |
| `0x0B` | read register | thread byte, register byte (0-7, 8 PC, 9 flags), dummy, word (halted threads only) |

Host IMEM accesses use cycles whose fetch slot belongs to a halted thread, so code can be loaded into a halted thread while others run.

## Verification hooks built into the architecture

- The model predicts every pad on every cycle, so RTL tests compare full pin traces and architectural commits.
- Counter and slot-length widths are parameters, so formal proofs run on narrowed lanes (8-bit counters, 4-bit lengths), with the data-independence argument written down in `docs/verification.md`.
- Every timing failure has a flag, so "no flag" is a meaningful precondition for the timing-independence theorem.
