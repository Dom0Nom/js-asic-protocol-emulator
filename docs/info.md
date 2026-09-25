<!---
Datasheet for Tiny Tapeout. Draft: the full instruction set reference and the
host loader will be added before submission.
-->

## How it works

Tempo is a small processor built for bit-banging protocols. Firmware decides what to send; timed hardware lanes decide exactly when pins move.

- **Four threads** share one 16-bit datapath in strict rotation (a barrel), each executing one instruction every 4 clock cycles. Instructions come from a 1024 x 16 SRAM loaded by the host.
- **Four lanes** each own up to four pins (data out, data in, clock, aux). A lane shifts words out and in on its own timeline, with a fractional bit period, so the pins keep exact timing no matter how the threads are scheduled. Lanes 0 and 1 also do NRZI, bit stuffing, CRC and clock recovery (enough for USB low speed); lanes 2 and 3 do plain NRZ.
- Each data bit is drawn as a programmable **symbol** (two levels and a split point inside the bit), so NRZ, Manchester, pulse-width codes such as WS2812 and 1-Wire, and pulse-distance codes are register settings.
- Lanes drive clock pins (SPI modes 0-3, I2C, JTAG, SWD), run open drain with clock stretching, can hard-start a receive on a pin pattern (UART start bit, USB SOP), and can timestamp every input edge.
- Missed deadlines, transmit underruns and receive overruns set sticky flags, so timing failures are visible instead of silent.

Pins: `uio[7:0]` are general bidirectional pins (index 0-7), `uo[6:0]` general outputs (index 8-14), `ui[4:0]` general inputs (index 8-12). `ui[7:5]` and `uo[7]` are the host port.

## How to test

The host (the demo board RP2040) talks to Tempo over SPI mode 0 on `ui[7]` (CS, active low), `ui[6]` (SCK, at most clk/12), `ui[5]` (MOSI) and `uo[7]` (MISO). Every transaction starts with a command byte; values are big endian.

| Command | Bytes after the command | Effect |
|---|---|---|
| `0x01` | address (2), then 16-bit words | write instruction memory |
| `0x02` | address (2), dummy, then read words | read instruction memory |
| `0x05` | 1: `[3:0]` threads to start, `[7:4]` threads to stop | run control |
| `0x06` | thread, address (2) | set the PC of a halted thread |
| `0x07` | dummy, then read 4 status bytes | run and fault bits, mailbox levels, lane flags |
| `0x08` | 16-bit words | send words to the firmware mailbox |
| `0x09` | dummy, then read words | read words the firmware sent |
| `0x0A` | thread | single-step a halted thread |
| `0x0B` | thread, register, dummy, then read a word | read a register of a halted thread |

To run firmware: write the program image with `0x01`, set the start address of each thread with `0x06`, start the threads with `0x05`, then exchange data through the mailbox with `0x08` and `0x09`. Instruction memory powers up with random contents, so firmware must only read words it loaded.

## External hardware

None required. Protocol targets (UART adapters, SPI flash, I2C sensors with pull-ups, a USB low-speed connector with a 1.5 kOhm pull-up on D-) connect to the PMOD headers.
