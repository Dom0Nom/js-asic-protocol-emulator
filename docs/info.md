## How it works

Tempo is a small processor for bit-banging protocols. Four threads share one 16-bit datapath and a 1024 x 16 SRAM; each runs one instruction every four clock cycles. Three lanes move pins on their own timelines: a thread gives a lane a word and a bit count, and the lane shifts it with a fractional bit period, drives the clock pin, samples at a set offset and, on lanes 0 and 1, applies NRZI, bit stuffing and CRC. UART, SPI, I2C, JTAG, SWD, USB low-speed packets, PS/2, CAN transmit and Manchester are firmware in `fw/`. The instruction set and lane registers are in [architecture.md](architecture.md).

| Pin | Use |
|---|---|
| `uio[7:0]` | protocol pins 0-7, bidirectional, open drain selectable |
| `ui[4:0]` | protocol inputs 8-12 |
| `uo[2:0]`, `uo[7]`, `uo[6:4]` | protocol outputs 8-10, 11, 12-14 |
| `ui[5]`, `ui[6]`, `ui[7]` | host SPI clock, data in, chip select (active low) |
| `uo[3]` | host SPI data out |

The clock is <!-- clock:begin -->25 MHz<!-- clock:end -->. Every input passes a two-flop synchroniser and every output a pad register.

## How to test

The host loads firmware and exchanges data over SPI mode 0 with the clock at most clk/12. On the Tiny Tapeout demo board these pins are the RP2350's SPI0 (GPIO22 SCK, GPIO23 MOSI, GPIO24 CS, GPIO36 MISO). Each transaction starts with a command byte; words and addresses are big endian.

| Command | Bytes after the command | Effect |
|---|---|---|
| `0x01` | address (2), then words | write instruction memory |
| `0x02` | address (2), dummy, then read words | read instruction memory |
| `0x05` | `[3:0]` threads to start, `[7:4]` threads to stop | run control |
| `0x06` | thread, address (2) | set the PC of a halted thread |
| `0x07` | dummy, then read 8 bytes | run and fault bits, mailbox levels, lane flags |
| `0x08` | words | write the host-to-thread mailbox; the reply byte gives the free space |
| `0x09` | dummy, then read words | read the thread-to-host mailbox; the reply byte gives the level |
| `0x0A` | thread | single-step a halted thread |
| `0x0B` | thread, register, dummy, then read a word | read a register of a halted thread |

To run firmware: write the image with `0x01`, set each thread's start address with `0x06`, start threads with `0x05`, then exchange data with `0x08` and `0x09`. The SRAM powers up with random contents, so firmware must only read words it loaded.

`host/tempo_host/` does this from CPython or MicroPython. It needs a transport object with `xfer(bytes) -> bytes` (one chip-select-framed transaction), `sleep_us` and `ticks_us`. A UART on `uio0` (TX) and `uio1` (RX):

```python
from tempo_host.link import Link
from tempo_host.uart import Uart

link = Link(transport)          # transport: your SPI master
uart = Uart(link, tx=0, rx=1, baud=115200)
uart.write(b"hello\r\n")
print(uart.read(16, timeout_us=100000))
```

The same call sequence runs against the simulated chip: `python tools/tests/test_cli.py i2c` prints an I2C bus scan from the cycle model. The library has not run on hardware yet.

## External hardware

A Tiny Tapeout demo board supplies clock, reset and the host SPI master. Connect protocol targets to the `uio` PMOD at the board's IO voltage. I2C needs pull-ups on SDA and SCL. Pad and mux speed on the carrier limit real toggle rates; 10BASE-T is not supported.
