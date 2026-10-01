#!/usr/bin/env python3
"""I2C devices on the T-Deck Max.

Addresses come from the vendor's own lib/TDeckMaxBoard/src/TDeckMaxBoard.h, not
from datasheet guesswork.

Most of these follow the same shape -- address a register, then read or write
from it, auto-incrementing -- so `RegisterFile` covers them and only the parts
the firmware actually inspects get specialised.
"""

from __future__ import annotations

import logging

from emu.peripherals.i2c import I2CDevice

from emu.devices.bhi260ap import BHI260AP  # noqa: E402  (ADR 0108)

log = logging.getLogger("i2c.dev")

# The real part's key-event FIFO is ten deep.
FIFO_DEPTH = 10


class RegisterFile(I2CDevice):
    """The common register-pointer device.

    A write sets the pointer from its first byte and stores the rest; a read
    returns bytes from the pointer onward. Devices that need more than this
    override `on_write`.
    """

    def __init__(self, address: int, name: str, size: int = 256,
                 defaults: dict[int, int] | None = None):
        self.address = address
        self.name = name
        self.regs = bytearray(size)
        self.defaults = defaults or {}
        for reg, val in self.defaults.items():
            self.regs[reg] = val
        self.ptr = 0
        self._expect_pointer = False
        self.writes = 0
        self.reads = 0

    def start(self, reading: bool) -> None:
        # A repeated START for the read half of a register read must not
        # disturb the pointer the write half just set.
        if not reading:
            self._expect_pointer = True

    def write(self, data: bytes) -> bool:
        if not data:
            return True
        if self._expect_pointer:
            self.ptr = data[0] % len(self.regs)
            data = data[1:]
            self._expect_pointer = False
        for b in data:
            self.regs[self.ptr] = b
            self.on_write(self.ptr, b)
            self.ptr = (self.ptr + 1) % len(self.regs)
        self.writes += 1
        return True

    def read(self, count: int) -> bytes:
        out = bytes(self.regs[(self.ptr + i) % len(self.regs)] for i in range(count))
        self.ptr = (self.ptr + count) % len(self.regs)
        self.reads += 1
        return out

    def on_write(self, reg: int, value: int) -> None:
        """Hook for devices with side effects."""

    def __repr__(self) -> str:
        return f"<{self.name}@0x{self.address:02x} r={self.reads} w={self.writes}>"


class XL9555(RegisterFile):
    """16-bit I/O expander, TCA9555 register layout.

    Inputs read back as whatever the outputs were last driven to, which is what
    a board with no external stimulus behaves like and keeps firmware that
    reads back its own writes happy.
    """

    IN0, IN1, OUT0, OUT1, POL0, POL1, CFG0, CFG1 = range(8)

    def __init__(self, address: int = 0x20):
        super().__init__(address, "XL9555", size=8,
                         defaults={self.OUT0: 0xFF, self.OUT1: 0xFF,
                                   self.CFG0: 0xFF, self.CFG1: 0xFF})

    def on_write(self, reg: int, value: int) -> None:
        if reg == self.OUT0:
            self.regs[self.IN0] = value
        elif reg == self.OUT1:
            self.regs[self.IN1] = value


class TCA8418(RegisterFile):
    """Keyboard matrix controller.

    The firmware never polls this: it configures the part and then waits on a
    GPIO interrupt (ADR 0010). So a key press is two things -- an entry in the
    event FIFO here, and a falling edge on the controller's INT line, which the
    board wiring raises separately.
    """

    CFG = 0x01
    INT_STAT = 0x02
    KEY_LCK_EC = 0x03
    KEY_EVENT_A = 0x04

    COLS = 10

    # Physical layout of the T-Deck Max keyboard, row by row. A key's position
    # in this grid is what the controller reports; the character is only how the
    # host names it.
    # Verbatim from examples/factory/peri_keypad.cpp. Note row 2 starts with
    # ALT and ends with ENTER, and row 3 has only five wired keys -- the
    # first five positions are not populated and the firmware drops them.
    LAYOUT = (
        ("Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P"),
        ("A", "S", "D", "F", "G", "H", "J", "K", "L", "DEL"),
        ("ALT", "Z", "X", "C", "V", "B", "N", "M", "$", "ENTER"),
        (None, None, None, None, None, "UP", "0", "SPACE", "SYM", "UP2"),
    )

    def __init__(self, address: int = 0x34):
        super().__init__(address, "TCA8418", size=64)
        self.pending: list[int] = []
        self.keymap = self._build_keymap()

    @classmethod
    def _build_keymap(cls) -> dict[str, tuple[int, int]]:
        table: dict[str, tuple[int, int]] = {}
        for row, keys in enumerate(cls.LAYOUT):
            for col, name in enumerate(keys):
                if name is not None:
                    table.setdefault(name.upper(), (row, col))
        return table

    def key_code(self, row: int, col: int) -> int:
        """The controller numbers keys from 1 across the matrix. The firmware
        recovers the column as (COLS-1) - k % COLS, so the numbering runs right
        to left within a row."""
        return row * self.COLS + (self.COLS - 1 - col) + 1

    def key_event(self, key: str, down: bool = True) -> bool:
        position = self.keymap.get(key.upper())
        if position is None:
            return False
        code = self.key_code(*position)
        self.pending.append(code | (0x80 if down else 0x00))
        return True

    def read(self, count: int) -> bytes:
        # These two registers are computed at read time rather than stored: the
        # count has to fall as the driver drains the FIFO, or it loops forever.
        if self.ptr == self.KEY_EVENT_A:
            self.regs[self.KEY_EVENT_A] = self.pending.pop(0) if self.pending else 0
        elif self.ptr == self.KEY_LCK_EC:
            self.regs[self.KEY_LCK_EC] = min(len(self.pending), FIFO_DEPTH)
        elif self.ptr == self.INT_STAT:
            self.regs[self.INT_STAT] = 0x01 if self.pending else 0x00
        return super().read(count)


class CST328(I2CDevice):
    """Capacitive touch panel, 240x320.

    Unlike the other devices here this one is addressed with a 16-bit register
    number and validates its own reports, so it cannot reuse RegisterFile.

    Two details are load-bearing. The last byte of a report must be 0xAB or the
    driver discards it, and the driver probes for a cst66xx at the same address
    first -- that probe is rejected by returning anything other than 0xCA at
    bytes 2 and 3, which is what lets it fall through to the CST328 path.
    """

    REG_REPORT = 0xD000
    REG_CHIP_INFO = 0xD1F4
    REPORT_MAGIC = 0xAB
    CONTACT = 0x06

    def __init__(self, address: int = 0x1A):
        self.address = address
        self.name = "CST328"
        self.reg = 0
        self._written = b""
        self._out = b""
        self._out_pos = 0
        self.touch: tuple[int, int] | None = None
        self.reports = 0
        self.acks = 0

    def start(self, reading: bool) -> None:
        # Deliberately does not re-latch on a read. The reply is built when the
        # register is selected, and a read simply continues through it, which is
        # what a real device's internal pointer does.
        #
        # This matters because one logical read reaches us as more than one
        # call: the controller splits it into n-1 acknowledged bytes plus a
        # final unacknowledged one, and the driver may split it again across
        # command lists. Rebuilding per call hands out byte 0 where byte 6 was
        # expected -- and for this device that is silent, because it checks the
        # last byte for 0xAB, discards the whole report when it does not match,
        # and still writes its acknowledgement.
        if not reading:
            self._written = b""

    def _build(self) -> bytes:
        if self.reg == self.REG_REPORT:
            return self._report_bytes()
        if self.reg == self.REG_CHIP_INFO:
            return self._chip_info_bytes()
        return bytes(64)

    def write(self, data: bytes) -> bool:
        self._written += data
        if len(self._written) >= 2:
            self.reg = (self._written[0] << 8) | self._written[1]
        # A three-byte write of 0xAB to the report register is the driver
        # acknowledging what it just read; on real hardware that releases INT.
        if (len(self._written) == 3 and self.reg == self.REG_REPORT
                and self._written[2] == self.REPORT_MAGIC):
            self.acks += 1
        if len(self._written) >= 2:
            self._out = self._build()
            self._out_pos = 0
        return True

    def read(self, count: int) -> bytes:
        out = self._out[self._out_pos:self._out_pos + count]
        self._out_pos += count
        return out + bytes(count - len(out))

    def _report_bytes(self) -> bytes:
        buf = bytearray(16)
        if self.touch is not None:
            x, y = self.touch
            buf[0] = self.CONTACT            # finger 0, contact event
            buf[1] = (x >> 4) & 0xFF
            buf[2] = (y >> 4) & 0xFF
            buf[3] = ((x & 0x0F) << 4) | (y & 0x0F)
            buf[4] = 0x3F                    # pressure
            buf[5] = 0x01                    # one finger, not a key event
        buf[6] = self.REPORT_MAGIC
        self.reports += 1
        return bytes(buf)

    def _chip_info_bytes(self) -> bytes:
        info = bytearray(32)
        info[4:6] = (240).to_bytes(2, "little")   # reported x resolution
        info[6:8] = (320).to_bytes(2, "little")   # reported y resolution
        info[18:20] = (0x0328).to_bytes(2, "little")
        return bytes(info)

    def __repr__(self) -> str:
        return f"<CST328@0x{self.address:02x} reports={self.reports} acks={self.acks}>"


class DRV2605(RegisterFile):
    """Haptic driver. Only the status register matters at init: bits 7:5 carry
    the device id, and the driver refuses to continue if they read back as 0."""

    def __init__(self, address: int = 0x5A):
        super().__init__(address, "DRV2605", size=0x40, defaults={0x00: 0xE0})


class SY6970(RegisterFile):
    """Charger/PMIC. Register 0x14 bit 7 is the reset bit and bits 5:3 are the
    part number; leaving them zero makes probing code think the part is absent,
    so a plausible id is supplied."""

    def __init__(self, address: int = 0x6A):
        super().__init__(address, "SY6970", size=0x15,
                         defaults={0x0B: 0x40, 0x14: 0x30})


class BQ27220(RegisterFile):
    """Fuel gauge: a battery that is charged and healthy, behind the access
    control and configuration protocol a driver uses to provision it.

    The protocol is what the T-Deck Max's own driver does (lib/BQ27220 in the
    vendor's repository), with its codes from that driver's bq27220_def.h:

    - A subcommand is a little-endian word written to Control() (0x00) or
      MACSubcmd() (0x3E). DEVICE_NUMBER answers 0x0220 in MACData() (0x40);
      the factory demo gave the gauge up as "Invalid Device Number 0000 !=
      0x0220" while this answered nothing.
    - Reading Control() itself gives CONTROL_STATUS, whose BATT_ID (bits
      0..2) is the active profile, 0 by default.
    - OperationStatus() (0x3A) carries the security mode in bits 1..2 (3
      sealed, 2 unsealed, 1 full access), INITCOMP in bit 5 and CFGUPDATE in
      bit 10. The two unseal keys in turn unseal it, SEALED seals it, RESET
      reinitialises it, and ENTER_CFG_UPDATE / EXIT_CFG_UPDATE[_REINIT] put it
      in and out of configuration mode. The driver waits on each of these bits.
    - Data memory is addressed through MACSubcmd(): an address alone selects
      it for reading back through MACData(); an address followed by data is
      staged, and written when MACDataSum() (0x60) arrives with the checksum
      (0xFF minus the byte sum of address and data) and the length (+4).
      Data memory is flash on the real part; here it starts empty each run,
      as a gauge does before its first provisioning.
    """

    CONTROL = 0x00
    OPERATION_STATUS = 0x3A
    MAC_SUBCMD = 0x3E
    MAC_DATA = 0x40
    MAC_DATA_SUM = 0x60
    MAC_DATA_LEN = 0x61
    MAC_BLOCK = 32

    DEVICE_NUMBER = 0x0001
    SEALED = 0x0030
    RESET = 0x0041
    ENTER_CFG_UPDATE = 0x0090
    EXIT_CFG_UPDATE_REINIT = 0x0091
    EXIT_CFG_UPDATE = 0x0092
    UNSEAL_KEY_1, UNSEAL_KEY_2 = 0x0414, 0x3672
    FULL_ACCESS_KEY = 0xFFFF

    SEC_FULL, SEC_UNSEALED, SEC_SEALED = 1, 2, 3
    CONTROL_STATUS = 0x0000
    INITCOMP = 1 << 5
    CFGUPDATE = 1 << 10
    # Data memory addresses start here; a word below it is a subcommand.
    DATA_MEMORY = 0x4000

    def __init__(self, address: int = 0x55):
        super().__init__(address, "BQ27220", size=0x80, defaults={
            0x06: 0xA5, 0x07: 0x0B,   # temperature 2981 x 0.1 K, 25.0 C
            0x08: 0x10, 0x09: 0x10,   # voltage  ~4112 mV
            0x0A: 0x09, 0x0B: 0x00,   # battery status: present, discharging
            0x10: 0xF6, 0x11: 0x09,   # remaining capacity 2550 mAh
            0x12: 0xB8, 0x13: 0x0B,   # full charge capacity 3000 mAh
            0x2C: 0x55, 0x2D: 0x00,   # state of charge 85 %
            0x2E: 0x64, 0x2F: 0x00,   # state of health 100 %
            0x3C: 0xB8, 0x3D: 0x0B,   # design capacity 3000 mAh
        })
        self.security = self.SEC_SEALED
        self.status = self.INITCOMP
        self.memory: dict[int, int] = {}
        self.staged: tuple[int, bytes] | None = None
        self._last_key = 0
        self._publish_status()

    def write(self, data: bytes) -> bool:
        reg = data[0] if self._expect_pointer and data else self.ptr
        payload = data[1:] if self._expect_pointer and data else data
        ok = super().write(data)
        if reg in (self.CONTROL, self.MAC_SUBCMD) and len(payload) >= 2:
            word = payload[0] | (payload[1] << 8)
            if reg == self.MAC_SUBCMD and word >= self.DATA_MEMORY:
                self._select(word, bytes(payload[2:]))
            else:
                self._subcommand(word)
        elif reg == self.MAC_DATA_SUM and len(payload) >= 2:
            self._commit(payload[0], payload[1])
        self._publish_status()
        return ok

    # -- the protocol ----------------------------------------------------

    def _answer(self, word: int, answer: bytes) -> None:
        self.regs[self.MAC_DATA:self.MAC_DATA + self.MAC_BLOCK] = bytes(self.MAC_BLOCK)
        self.regs[self.MAC_DATA:self.MAC_DATA + len(answer)] = answer
        head = word.to_bytes(2, "little")
        self.regs[self.MAC_DATA_SUM] = 0xFF - (sum(head + answer) & 0xFF)
        self.regs[self.MAC_DATA_LEN] = len(answer) + 4

    def _subcommand(self, word: int) -> None:
        key, self._last_key = self._last_key, word
        if word == self.DEVICE_NUMBER:
            self._answer(word, (0x0220).to_bytes(2, "little"))
        elif word == self.UNSEAL_KEY_2 and key == self.UNSEAL_KEY_1:
            if self.security == self.SEC_SEALED:
                self.security = self.SEC_UNSEALED
        elif word == self.FULL_ACCESS_KEY and key == self.FULL_ACCESS_KEY:
            if self.security == self.SEC_UNSEALED:
                self.security = self.SEC_FULL
        elif word == self.SEALED:
            self.security = self.SEC_SEALED
        elif word == self.RESET:
            # Reinitialised at once: the driver only waits for INITCOMP.
            self.status = (self.status & ~self.CFGUPDATE) | self.INITCOMP
        elif word == self.ENTER_CFG_UPDATE:
            if self.security != self.SEC_SEALED:
                self.status |= self.CFGUPDATE
        elif word in (self.EXIT_CFG_UPDATE, self.EXIT_CFG_UPDATE_REINIT):
            self.status &= ~self.CFGUPDATE
        else:
            self._answer(word, b"\x00\x00")

    def _select(self, address: int, data: bytes) -> None:
        if data:
            self.staged = (address, data)
            return
        self.staged = None
        block = bytes(self.memory.get(address + i, 0) for i in range(self.MAC_BLOCK))
        self._answer(address, block)

    def _commit(self, checksum: int, length: int) -> None:
        if self.staged is None:
            return
        address, data = self.staged
        self.staged = None
        expected = 0xFF - (sum(address.to_bytes(2, "little") + data) & 0xFF)
        if checksum != expected or length != len(data) + 4:
            log.warning("BQ27220 data memory write at 0x%04x refused: checksum "
                        "0x%02x/%d, expected 0x%02x/%d", address, checksum, length,
                        expected, len(data) + 4)
            return
        if not self.status & self.CFGUPDATE:
            log.warning("BQ27220 data memory write at 0x%04x outside config update",
                        address)
            return
        for i, b in enumerate(data):
            self.memory[address + i] = b

    def _publish_status(self) -> None:
        word = self.status | (self.security << 1)
        self.regs[self.OPERATION_STATUS] = word & 0xFF
        self.regs[self.OPERATION_STATUS + 1] = word >> 8
        # Reading Control() gives CONTROL_STATUS, not the subcommand last
        # written there. Its BATT_ID (bits 0..2) is the active profile: 0, the
        # default, which is what the driver checks before it trusts the gauge.
        self.regs[self.CONTROL] = self.CONTROL_STATUS & 0xFF
        self.regs[self.CONTROL + 1] = self.CONTROL_STATUS >> 8


class ES8311(RegisterFile):
    """Audio codec. Register 0xFD/0xFE hold the chip id (0x83, 0x11)."""

    def __init__(self, address: int = 0x18):
        super().__init__(address, "ES8311", size=0x100,
                         defaults={0xFD: 0x83, 0xFE: 0x11})


def tdeck_max_devices(clock=None) -> list[I2CDevice]:
    return [
        ES8311(0x18),
        CST328(0x1A),
        XL9555(0x20),
        BHI260AP(0x28, clock=clock),
        TCA8418(0x34),
        BQ27220(0x55),
        DRV2605(0x5A),
        SY6970(0x6A),
    ]
