<!-- Datasheet for Tiny Tapeout. Physical timing closure remains in progress. -->

## How it works

Tempo is a small processor built for bit-banging protocols. Firmware decides what to send; timed hardware lanes decide exactly when pins move.

- **Four threads** share one 16-bit datapath in strict rotation (a barrel), each executing one instruction every 4 clock cycles. Instructions come from a 1024 x 16 SRAM loaded by the host.
- **Three lanes** each select data out, data in, clock and aux pins. A lane shifts words on its own timeline with a fractional bit period. Lanes 0 and 1 also do NRZI, bit stuffing, CRC and receive resynchronisation; lane 2 does plain NRZ. Lane 3 instructions halt the issuing thread as illegal.
- Each data bit is drawn as a programmable **symbol** (two levels and a split point inside the bit), so NRZ, Manchester, pulse-width codes such as WS2812 and 1-Wire, and pulse-distance codes are register settings.
- Lanes drive clock pins (SPI modes 0-3, I2C, JTAG, SWD), run open drain with clock stretching, can hard-start a receive on a pin pattern (UART start bit, USB SOP), and can timestamp every input edge.
- Missed deadlines, transmit underruns and receive overruns set sticky flags, so timing failures are visible instead of silent.

`uio[7:0]` are bidirectional protocol pins (indices 0–7), and `ui[4:0]`
are general inputs (indices 8–12). Output indices 8–10 map to `uo[0:2]`,
index 11 maps to `uo[7]`, and indices 12–14 map to `uo[4:6]`.
`ui[7:5]` and `uo[3]` carry the host port. Pin selector 15 disables a lane
pin; it cannot select the host port.

The target core clock is 50 MHz (20 ns). The last completed routed design
misses slow-corner setup timing; a safe production clock is not signed off
yet. `Link.clock_hz` must match the clock the board actually supplies.

## How to test

The demo-board RP2350 talks to Tempo over SPI mode 0 on `ui[7]` (CS, active
low), `ui[5]` (SCK, at most clk/12), `ui[6]` (MOSI) and `uo[3]` (MISO).
These are RP2350 GPIO24, GPIO22, GPIO23 and GPIO36 respectively. Every
transaction starts with a command byte; words and addresses are big endian.

| Command | Bytes after the command | Effect |
|---|---|---|
| `0x01` | address (2), then 16-bit words | write instruction memory |
| `0x02` | address (2), dummy, then read words | read instruction memory |
| `0x05` | 1: `[3:0]` threads to start, `[7:4]` threads to stop | run control |
| `0x06` | thread, address (2) | set the PC of a halted thread |
| `0x07` | dummy, then read 8 status bytes | run and fault bits, mailbox levels, aggregate flags, thread LATE bits, four lane status bytes (lane 3 is zero) |
| `0x08` | 16-bit words | the reply byte after the command reports free H2T space; only that many words are accepted |
| `0x09` | dummy, then read words | the reply during the dummy byte reports T2H level; only that many words are popped |
| `0x0A` | thread | single-step a halted thread |
| `0x0B` | thread, register, dummy, then read a word | read a register of a halted thread |

To run firmware: write the program image with `0x01`, set the start address of each thread with `0x06`, start the threads with `0x05`, then exchange data through the mailbox with `0x08` and `0x09`. Instruction memory powers up with random contents, so firmware must only read words it loaded.

Mailbox count replies also carry an error-summary bit in bit 7. Mask the
count with 7, retry unaccepted writes, and poll for additional read words.
The host library implements this contract.

Copy `host/tempo_host/` onto a Tiny Tapeout demo board with its `ttboard`
MicroPython firmware. Select Tempo using its shuttle project name:

```python
from tempo_host.rp2350 import board
from tempo_host.uart import Uart

link = board(clock_hz=25000000)
uart = Uart(link, tx=0, rx=1, baud=115200)
uart.write(b"hello\r\n")
```

Connect `uio0` to the adapter's RX, `uio1` to its TX, and share ground.
The 25 MHz example is an exact PWM divisor at the RP2350's 150 MHz default
system clock; it is an example clock, not a silicon validation result.
The host transport and protocol sessions have been checked in simulation
and under MicroPython; a physical board run remains outstanding.

The SPI API supports modes 0–3, with `msb_first=False` for LSB-first
devices. The cycle-model minimum period is 5 core cycles in modes 0/2 and
6 in modes 1/3. Maximum period is 8191 cycles; the default sample is at
`period - 1`. Real pad and target delays may require a longer period.
Bulk flash reads pipeline transfers into two SRAM buffer halves so the
thread can store one word while the lane receives the next.

## External hardware

A Tiny Tapeout carrier supplies clock, reset and host access. Use protocol
targets at the board's supported voltage and connect them to the PMOD pins.
I2C needs SDA/SCL pull-ups; SPI targets should hold CS high during reset.
USB low-speed PHY simulation does not establish enumeration or electrical
compliance on the Tiny Tapeout pads. IHP mux and pad speed limits remain
unmeasured; 10BASE-T is not a supported hardware claim.
