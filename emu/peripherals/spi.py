#!/usr/bin/env python3
"""ESP32-S3 general-purpose SPI master (SPI2/SPI3).

Register offsets from components/soc/esp32s3/include/soc/spi_reg.h.

Master mode with the CPU-driven data buffer only. DMA is not modelled: the
Arduino SPI library the firmware uses fills W0..W15 directly, and adding a DMA
path before anything needs it would be speculation.
"""

from __future__ import annotations

import logging

from emu.bus import Region

log = logging.getLogger("spi")

R_CMD = 0x00
R_ADDR = 0x04
R_CTRL = 0x08
R_CLOCK = 0x0C
R_USER = 0x10
R_USER1 = 0x14
R_USER2 = 0x18
R_MS_DLEN = 0x1C
R_MISC = 0x20
R_DMA_CONF = 0x30
R_W0 = 0x98
R_W15 = 0xD4

CMD_UPDATE = 1 << 23
CMD_USR = 1 << 24

USER_USR_MOSI = 1 << 27
USER_USR_MISO = 1 << 28
USER_USR_ADDR = 1 << 30
USER_USR_COMMAND = 1 << 31

MASK32 = 0xFFFFFFFF


class SPIDevice:
    """A device on the SPI bus. `selected` is consulted per transfer so a model
    can decide from chip-select state whether the bytes are for it."""

    name = "spi-device"

    def selected(self) -> bool:
        return True

    def transfer(self, data: bytes) -> bytes:
        return b"\xff" * len(data)


class SPIController(Region):
    def __init__(self, name: str, base: int, devices: list[SPIDevice] | None = None,
                 irq: int = -1):
        super().__init__(name, base, 0x1000, irq=irq)
        self.devices = devices or []
        self.regs: dict[int, int] = {}
        self.buf = bytearray(64)  # W0..W15
        self.transfers = 0
        self.bytes_out = 0

    def read(self, offset: int, size: int) -> int:
        if R_W0 <= offset <= R_W15 + 3:
            i = offset - R_W0
            return int.from_bytes(self.buf[i:i + 4], "little")
        return self.regs.get(offset, 0)

    def write(self, offset: int, value: int, size: int) -> None:
        value &= MASK32
        if R_W0 <= offset <= R_W15 + 3:
            i = offset - R_W0
            self.buf[i:i + 4] = value.to_bytes(4, "little")
            return
        if offset == R_CMD:
            # USR and UPDATE are self-clearing triggers. Storing them verbatim
            # makes the driver's poll for completion never finish, which is
            # indistinguishable from a hung peripheral.
            self.regs[offset] = value & ~(CMD_USR | CMD_UPDATE)
            if value & CMD_USR:
                self._transfer()
            return
        self.regs[offset] = value

    def _transfer(self) -> None:
        user = self.regs.get(R_USER, 0)
        nbits = (self.regs.get(R_MS_DLEN, 0) & 0x3FFFF) + 1
        nbytes = (nbits + 7) // 8

        out = bytes(self.buf[:nbytes]) if user & USER_USR_MOSI else b""
        self.transfers += 1
        self.bytes_out += len(out)

        target = next((d for d in self.devices if d.selected()), None)
        if target is None:
            if user & USER_USR_MISO:
                self.buf[:nbytes] = b"\x00" * nbytes
            return

        got = target.transfer(out) if out else target.transfer(b"\x00" * nbytes)
        if user & USER_USR_MISO and got:
            n = min(len(got), nbytes, len(self.buf))
            self.buf[:n] = got[:n]
