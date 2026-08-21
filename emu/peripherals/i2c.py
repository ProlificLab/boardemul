#!/usr/bin/env python3
"""ESP32-S3 I2C master controller.

Register offsets and bit positions are taken from ESP-IDF's
components/soc/esp32s3/include/soc/i2c_reg.h, not from memory.

Only master mode is implemented, because that is all the firmware uses. Slave
mode, arbitration loss and clock stretching are absent by choice rather than
oversight -- see the guard in `_execute`.
"""

from __future__ import annotations

import logging
from collections import deque

from emu.bus import Region

log = logging.getLogger("i2c")

R_SCL_LOW_PERIOD = 0x00
R_CTR = 0x04
R_SR = 0x08
R_TO = 0x0C
R_SLAVE_ADDR = 0x10
R_FIFO_ST = 0x14
R_FIFO_CONF = 0x18
R_DATA = 0x1C
R_INT_RAW = 0x20
R_INT_CLR = 0x24
R_INT_ENA = 0x28
R_INT_STATUS = 0x2C
R_CLK_CONF = 0x54
R_COMD0 = 0x58
R_DATE = 0xF8
TXFIFO_BASE = 0x100
RXFIFO_BASE = 0x180

CTR_MS_MODE = 1 << 4
CTR_TRANS_START = 1 << 5
CTR_FSM_RST = 1 << 10
CTR_CONF_UPGATE = 1 << 11

INT_RXFIFO_WM = 1 << 0
INT_TXFIFO_WM = 1 << 1
INT_END_DETECT = 1 << 3
INT_ARBITRATION_LOST = 1 << 5
INT_TRANS_COMPLETE = 1 << 7
INT_TIME_OUT = 1 << 8
INT_TRANS_START = 1 << 9
INT_NACK = 1 << 10

FIFO_CONF_NONFIFO_EN = 1 << 10
FIFO_CONF_RX_FIFO_RST = 1 << 12
FIFO_CONF_TX_FIFO_RST = 1 << 13

# ESP32-S3 opcodes differ from the ESP32's: see I2C_LL_CMD_* in
# components/hal/esp32s3/include/hal/i2c_ll.h.
CMD_RSTART, CMD_WRITE, CMD_READ, CMD_STOP, CMD_END = 6, 1, 3, 2, 4
CMD_DONE = 1 << 31

FIFO_DEPTH = 32


class I2CDevice:
    """A device on the bus. `write` returns False to NACK."""

    address: int = -1

    def start(self, reading: bool) -> None:
        """Called when the device is addressed, before any data moves."""

    def write(self, data: bytes) -> bool:
        return True

    def read(self, count: int) -> bytes:
        return b"\xff" * count

    def stop(self) -> None:
        """Called at the STOP condition."""


class I2CController(Region):
    def __init__(self, name: str, base: int, devices: list[I2CDevice] | None = None,
                 irq: int = -1):
        super().__init__(name, base, 0x1000, irq=irq)
        self.devices: dict[int, I2CDevice] = {d.address: d for d in (devices or [])}
        self.regs: dict[int, int] = {}
        self.comd = [0] * 8
        self.txfifo = bytearray()
        self.rxfifo = bytearray()
        self.int_raw = 0
        self.current: I2CDevice | None = None
        self.current_addr = -1
        self.transactions = 0
        self.nacks: set[int] = set()
        self.acks: set[int] = set()
        # Guessing which model returned a bad value is slow; reading the
        # last few transactions off the wire is not.
        self.history: deque[tuple[int, str, bytes]] = deque(maxlen=80)

    def irq_level(self) -> int:
        return 1 if (self.int_raw & self.regs.get(R_INT_ENA, 0)) else 0

    # -- register interface -------------------------------------------------

    def read(self, offset: int, size: int) -> int:
        if TXFIFO_BASE <= offset < TXFIFO_BASE + FIFO_DEPTH:
            return 0
        if RXFIFO_BASE <= offset < RXFIFO_BASE + FIFO_DEPTH:
            i = offset - RXFIFO_BASE
            return self.rxfifo[i] if i < len(self.rxfifo) else 0
        if R_COMD0 <= offset < R_COMD0 + 32:
            return self.comd[(offset - R_COMD0) // 4]

        if offset == R_DATA:
            return self._pop_rx()
        if offset == R_INT_RAW:
            return self.int_raw
        if offset == R_INT_STATUS:
            return self.int_raw & self.regs.get(R_INT_ENA, 0)
        if offset == R_FIFO_ST:
            return (min(len(self.rxfifo), 0x3F) << 8) | (min(len(self.txfifo), 0x3F) << 18)
        if offset == R_SR:
            # Bus is always idle by the time the guest looks: transactions
            # complete synchronously inside the TRANS_START write.
            return 0
        if offset == R_DATE:
            return 0x19071700
        return self.regs.get(offset, 0)

    def write(self, offset: int, value: int, size: int) -> None:
        if R_COMD0 <= offset < R_COMD0 + 32:
            self.comd[(offset - R_COMD0) // 4] = value
            return
        if TXFIFO_BASE <= offset < TXFIFO_BASE + FIFO_DEPTH:
            self._push_tx(value & 0xFF)
            return

        if offset == R_DATA:
            self._push_tx(value & 0xFF)
            return
        if offset == R_INT_CLR:
            self.int_raw &= ~value & 0xFFFFFFFF
            return
        if offset == R_FIFO_CONF:
            if value & FIFO_CONF_TX_FIFO_RST:
                self.txfifo.clear()
            if value & FIFO_CONF_RX_FIFO_RST:
                self.rxfifo.clear()
            self.regs[offset] = value
            return
        if offset == R_CTR:
            # TRANS_START, FSM_RST and CONF_UPGATE are self-clearing triggers.
            # Storing them verbatim makes the driver's own read-modify-write of
            # CTR re-fire the transaction, which corrupts the FIFOs and looks
            # exactly like a firmware bug.
            self.regs[offset] = value & ~(CTR_TRANS_START | CTR_FSM_RST
                                          | CTR_CONF_UPGATE)
            if value & CTR_FSM_RST:
                self.txfifo.clear()
                self.rxfifo.clear()
            if value & CTR_TRANS_START:
                self._execute()
            return
        self.regs[offset] = value

    # -- fifo ---------------------------------------------------------------

    def _push_tx(self, byte: int) -> None:
        if len(self.txfifo) < FIFO_DEPTH:
            self.txfifo.append(byte)

    def _pop_rx(self) -> int:
        return self.rxfifo.pop(0) if self.rxfifo else 0

    # -- command list -------------------------------------------------------

    def _execute(self) -> None:
        if not self.regs.get(R_CTR, 0) & CTR_MS_MODE:
            log.warning("%s: TRANS_START in slave mode, which is not modelled",
                        self.name)
            self.int_raw |= INT_TRANS_COMPLETE
            return

        self.int_raw &= ~(INT_NACK | INT_TRANS_COMPLETE | INT_END_DETECT)
        addressing = False
        nacked = False

        if log.isEnabledFor(logging.DEBUG):
            names = {CMD_RSTART: "START", CMD_WRITE: "WR", CMD_READ: "RD",
                     CMD_STOP: "STOP", CMD_END: "END"}
            decoded = []
            for c in self.comd:
                o = (c >> 11) & 0x7
                if c == 0:
                    continue
                decoded.append(f"{names.get(o, f'op{o}')}({c & 0xFF})")
            log.debug("%s exec [%s] tx=%s dev=0x%02x", self.name,
                      " ".join(decoded), bytes(self.txfifo).hex(" "),
                      self.current_addr & 0xFF)

        for slot in range(8):
            cmd = self.comd[slot]
            op = (cmd >> 11) & 0x7
            count = cmd & 0xFF
            ack_check = bool(cmd & (1 << 8))

            if op == CMD_RSTART:
                addressing = True
            elif op == CMD_WRITE:
                data = bytes(self.txfifo[:count])
                del self.txfifo[:count]
                if addressing and data:
                    nacked = not self._address(data[0], ack_check)
                    addressing = False
                    if not nacked and len(data) > 1 and self.current:
                        self.history.append((self.current_addr, "w", data[1:]))
                        nacked = not self.current.write(data[1:])
                elif self.current:
                    self.history.append((self.current_addr, "w", data))
                    nacked = not self.current.write(data)
                elif ack_check:
                    nacked = True
            elif op == CMD_READ:
                got = self.current.read(count) if self.current else b"\xff" * count
                self.rxfifo.extend(got[:count])
            elif op == CMD_STOP:
                if self.current:
                    self.current.stop()
                self.current, self.current_addr = None, -1
                self.comd[slot] |= CMD_DONE
                self.int_raw |= INT_TRANS_COMPLETE
                break
            elif op == CMD_END:
                # END splits a transaction across command-list refills; the
                # driver waits on END_DETECT and then writes more commands.
                self.comd[slot] |= CMD_DONE
                self.int_raw |= INT_END_DETECT
                break

            self.comd[slot] |= CMD_DONE
            if nacked:
                self.int_raw |= INT_NACK | INT_TRANS_COMPLETE
                break
        else:
            self.int_raw |= INT_TRANS_COMPLETE

        if self.rxfifo:
            self.int_raw |= INT_RXFIFO_WM
        self.transactions += 1
        log.debug("%s done int_raw=0x%04x ena=0x%04x rx=%s", self.name,
                  self.int_raw, self.regs.get(R_INT_ENA, 0),
                  bytes(self.rxfifo).hex(" "))

    def _address(self, addr_byte: int, ack_check: bool) -> bool:
        self.current_addr = addr_byte >> 1
        reading = bool(addr_byte & 1)
        self.current = self.devices.get(self.current_addr)
        if self.current is None:
            self.nacks.add(self.current_addr)
            log.debug("%s: no device at 0x%02x", self.name, self.current_addr)
            return not ack_check
        self.acks.add(self.current_addr)
        self.current.start(reading)
        return True
