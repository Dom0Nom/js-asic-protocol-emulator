# SPDX-License-Identifier: Apache-2.0
"""JEP106 manufacturer names for JTAG IDCODE and Arm DPIDR designer fields.

An 11-bit manufacturer field is (continuation-code count << 7) | 7-bit code
with the parity bit stripped (IDCODE bits 11:1, DPIDR DESIGNER). A short
list of common vendors, checked against OpenOCD src/helper/jep106.inc;
anything else prints as bank/code.
"""

NAMES = {
    0x009: "Intel", 0x015: "NXP", 0x017: "Texas Instruments", 0x01F: "Atmel",
    0x020: "STMicroelectronics", 0x021: "Lattice", 0x029: "Microchip", 0x042: "Macronix",
    0x049: "Xilinx", 0x05A: "Winbond", 0x06E: "Altera", 0x144: "Nordic Semiconductor",
    0x23B: "Arm", 0x348: "GigaDevice", 0x489: "SiFive", 0x493: "Raspberry Pi",
    0x612: "Espressif",
}


def name(mfg):
    """Vendor for an 11-bit JEP106 manufacturer field."""
    return NAMES.get(mfg, "bank %d code 0x%02x" % ((mfg >> 7) + 1, mfg & 0x7F))


def decode_idcode(idcode):
    """(version, part, manufacturer name) of a JTAG IDCODE."""
    return idcode >> 28, (idcode >> 12) & 0xFFFF, name((idcode >> 1) & 0x7FF)
