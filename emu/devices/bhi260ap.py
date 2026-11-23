# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The Bosch BHI260AP sensor hub on the T-Deck Max's I2C bus (ADR 0108).

A hub, not a sensor: it boots firmware the host uploads, and then serves
"virtual sensors" through FIFOs. Everything here is Bosch's host interface as
its BSD-licensed driver (BHY2 SensorAPI, as vendored by SensorLib, which the
factory demo uses) speaks it:

- Registers: product id 0x89 at 0x1C, chip id 0x70 at 0x2B, reset request at
  0x14, boot status at 0x25, kernel version at 0x20, interrupt status at 0x2D,
  host-interface and interrupt control at 0x06 and 0x07.
- Channel 0 (0x00) takes commands: a little-endian command and length, then
  the payload padded to four bytes, split over as many writes as the host
  likes. The firmware upload (0x0002) counts its length in words.
- A parameter read is the parameter's number with 0x1000 set; the answer goes
  to the status channel (0x03) as code, length and data, and raises bit 5 of
  the interrupt status.
- The FIFOs (0x01 wake-up, 0x02 non-wake-up) are read in transfers: the first
  two bytes of each are its length, and the register does not advance.
- A FIFO holds records: a sensor's id and its event, or system records --
  full timestamps (253, six bytes) and large deltas (252, three bytes), in
  units of 1/64000 s.

What this model chooses, because no source says otherwise, is stated once:
the board lies still and face up (1 g on z, no rotation), the sensors present
are the accelerometer and gyroscope in their pass-through, raw and corrected
forms, and the kernel version after boot is KERNEL_VERSION, a stand-in for a
number that is in the uploaded image's own build and not in any document.
"""

from __future__ import annotations

import logging
import struct
import time

from emu.peripherals.i2c import I2CDevice

log = logging.getLogger("bhi260ap")

PRODUCT_ID, CHIP_ID = 0x89, 0x70

REG_CHAN_CMD, REG_FIFO_W, REG_FIFO_NW, REG_STATUS = 0x00, 0x01, 0x02, 0x03
REG_HOST_INTF_CTRL, REG_HOST_INT_CTRL = 0x06, 0x07
REG_RESET_REQ, REG_HOST_STATUS = 0x14, 0x17
REG_PRODUCT_ID, REG_KERNEL_VERSION = 0x1C, 0x20
REG_BOOT_STATUS, REG_CHIP_ID, REG_INT_STATUS = 0x25, 0x2B, 0x2D

BOOT_HOST_INTERFACE_READY = 0x10
BOOT_FW_VERIFY_DONE = 0x20

INT_FIFO_W_DRDY, INT_FIFO_NW_DRDY, INT_STATUS_READY = 0x02, 0x08, 0x20

CMD_UPLOAD_TO_PROGRAM_RAM = 0x0002
CMD_BOOT_PROGRAM_RAM = 0x0003
CMD_CONFIG_SENSOR = 0x000D
PARAM_READ = 0x1000
PARAM_VIRT_SENSOR_PRESENT = 0x011F
PARAM_SENSOR_INFO_0 = 0x0300

TS_LARGE_DELTA, TS_FULL = 252, 253
TICKS_PER_SECOND = 64000

# A stand-in, not a measurement: see the module's docstring.
KERNEL_VERSION = 0x0001

ACCEL_IDS = (1, 3, 4)          # pass-through, raw, corrected
GYRO_IDS = (10, 12, 13)
EVENT_SIZE = 7                 # the id and three little-endian int16s
# The scale SensorLib applies to an accelerometer event is 1/4096 g.
ONE_G = 4096
# As much as a FIFO holds before the oldest records are lost.
FIFO_LIMIT = 4096


def _sensor_info(sensor_id: int) -> bytes:
    """The 28-byte answer to SENSOR_INFO for one sensor, at the offsets the
    driver reads: type, driver id and version, power, range, resolution, max
    rate, FIFO reserved and max, event size, min rate."""
    accel = sensor_id in ACCEL_IDS
    info = bytearray(28)
    info[0] = sensor_id
    struct.pack_into("<HH", info, 4, 8 if accel else 2000, 16)
    struct.pack_into("<f", info, 8, 400.0)
    struct.pack_into("<II", info, 12, 0, FIFO_LIMIT)
    info[20] = EVENT_SIZE
    struct.pack_into("<f", info, 21, 1.5625)
    return bytes(info)


class BHI260AP(I2CDevice):
    def __init__(self, address: int = 0x28, clock=None):
        self.address = address
        self.name = "BHI260AP"
        self._clock = clock
        self._started = time.monotonic()
        self.regs = bytearray(0x40)
        self.regs[REG_PRODUCT_ID] = PRODUCT_ID
        self.regs[REG_CHIP_ID] = CHIP_ID
        self.ptr = 0
        self._expect_pointer = False
        self.booted = False
        self.uploaded = 0
        self.enabled: dict[int, float] = {}      # sensor id -> rate in Hz
        self._next_sample: dict[int, float] = {}
        self._last_stamp: int | None = None
        self.fifos = {REG_FIFO_W: bytearray(), REG_FIFO_NW: bytearray()}
        self.status: list[bytes] = []
        self._transfer: dict[int, bytearray] = {}
        self._command: bytearray | None = None
        self._remaining = 0
        self.commands: list[int] = []
        self.unmodelled: list[str] = []
        self._reset()

    # -- the bus ---------------------------------------------------------

    def start(self, reading: bool) -> None:
        if not reading:
            self._expect_pointer = True

    def write(self, data: bytes) -> bool:
        if not data:
            return True
        if self._expect_pointer:
            self.ptr, data = data[0], data[1:]
            self._expect_pointer = False
        if not data:
            return True
        if self.ptr == REG_CHAN_CMD:
            self._channel(data)
        elif self.ptr == REG_RESET_REQ and data[0] & 1:
            self._reset()
        elif self.ptr < len(self.regs):
            for i, b in enumerate(data):
                if self.ptr + i < len(self.regs):
                    self.regs[self.ptr + i] = b
        return True

    def read(self, count: int) -> bytes:
        if self.ptr in (REG_FIFO_W, REG_FIFO_NW):
            return self._fifo_read(self.ptr, count)
        if self.ptr == REG_STATUS:
            return self._status_read(count)
        if self.ptr == REG_INT_STATUS:
            self._produce()
            self.regs[REG_INT_STATUS] = self._interrupt_status()
        out = bytes(self.regs[(self.ptr + i) % len(self.regs)] for i in range(count))
        self.ptr = (self.ptr + count) % len(self.regs)
        return out

    def stop(self) -> None:
        pass

    # -- boot ------------------------------------------------------------

    def _reset(self) -> None:
        self.booted = False
        self.uploaded = 0
        self.enabled.clear()
        self._next_sample.clear()
        self._last_stamp = None
        for fifo in self.fifos.values():
            fifo.clear()
        self.status.clear()
        self._transfer.clear()
        self._command, self._remaining = None, 0
        self.regs[REG_BOOT_STATUS] = BOOT_HOST_INTERFACE_READY
        self.regs[REG_KERNEL_VERSION:REG_KERNEL_VERSION + 2] = b"\x00\x00"

    # -- channel 0 -------------------------------------------------------

    def _channel(self, data: bytes) -> None:
        if self._command is None:
            if len(data) < 4:
                log.warning("BHI260AP: a command shorter than its header, dropped")
                return
            cmd, length = struct.unpack_from("<HH", data)
            payload = length * 4 if cmd == CMD_UPLOAD_TO_PROGRAM_RAM else length
            self._command = bytearray(data[:4])
            self._remaining = (payload + 3) & ~3
            data = data[4:]
        take = data[:self._remaining]
        self._remaining -= len(take)
        if self._command[:2] != struct.pack("<H", CMD_UPLOAD_TO_PROGRAM_RAM):
            self._command += take
        else:
            self.uploaded += len(take)
        if self._remaining <= 0:
            command, self._command = bytes(self._command), None
            self._execute(command)

    def _execute(self, command: bytes) -> None:
        cmd, length = struct.unpack_from("<HH", command)
        payload = command[4:4 + length]
        self.commands.append(cmd)
        if cmd == CMD_UPLOAD_TO_PROGRAM_RAM:
            # The hub verifies an image as it is uploaded, and the driver
            # checks for that before it asks for a boot: ready, verified, no
            # verify error.
            if self.uploaded:
                self.regs[REG_BOOT_STATUS] = BOOT_HOST_INTERFACE_READY | BOOT_FW_VERIFY_DONE
            log.info("BHI260AP: %d byte(s) of firmware uploaded", self.uploaded)
        elif cmd == CMD_BOOT_PROGRAM_RAM:
            self.booted = bool(self.regs[REG_BOOT_STATUS] & BOOT_FW_VERIFY_DONE)
            if self.booted:
                self.regs[REG_KERNEL_VERSION:REG_KERNEL_VERSION + 2] = \
                    struct.pack("<H", KERNEL_VERSION)
            log.info("BHI260AP: booted from RAM: %s", self.booted)
        elif cmd == CMD_CONFIG_SENSOR and len(payload) >= 8:
            sensor = payload[0]
            (rate,) = struct.unpack_from("<f", payload, 1)
            if sensor in ACCEL_IDS + GYRO_IDS and rate > 0:
                self.enabled[sensor] = rate
                self._next_sample.pop(sensor, None)
            else:
                self.enabled.pop(sensor, None)
            log.info("BHI260AP: sensor %d at %.1f Hz", sensor, rate)
        elif cmd & PARAM_READ and 0x0100 <= (cmd & 0x0FFF):
            self._parameter(cmd & 0x0FFF)
        else:
            self.unmodelled.append(f"0x{cmd:04x}")
            log.info("BHI260AP: command 0x%04x (%d byte(s)), no model", cmd, length)

    def _parameter(self, param: int) -> None:
        if param == PARAM_VIRT_SENSOR_PRESENT:
            present = bytearray(32)
            for sensor in ACCEL_IDS + GYRO_IDS:
                present[sensor // 8] |= 1 << (sensor % 8)
            answer = bytes(present)
        elif PARAM_SENSOR_INFO_0 < param < PARAM_SENSOR_INFO_0 + 0x100 \
                and param - PARAM_SENSOR_INFO_0 in ACCEL_IDS + GYRO_IDS:
            answer = _sensor_info(param - PARAM_SENSOR_INFO_0)
        else:
            self.unmodelled.append(f"param 0x{param:04x}")
            log.info("BHI260AP: parameter 0x%04x, no model", param)
            answer = b""
        self.status.append(struct.pack("<HH", param, len(answer)) + answer)

    # -- the sensors -----------------------------------------------------

    def _now(self) -> float | None:
        if self._clock is None:
            return time.monotonic() - self._started
        return self._clock()

    def _produce(self) -> None:
        """Every sample due since the last look, in time order, timestamped."""
        now = self._now()
        if now is None or not self.enabled or not self.booted:
            return
        due: list[tuple[float, int]] = []
        for sensor, rate in self.enabled.items():
            at = self._next_sample.setdefault(sensor, now)
            while at <= now:
                due.append((at, sensor))
                at += 1.0 / rate
            self._next_sample[sensor] = at
        fifo = self.fifos[REG_FIFO_NW]
        for at, sensor in sorted(due):
            stamp = int(at * TICKS_PER_SECOND)
            if self._last_stamp is None or not 0 <= stamp - self._last_stamp <= 0xFFFF:
                fifo += bytes([TS_FULL]) + stamp.to_bytes(5, "little")
            elif stamp != self._last_stamp:
                fifo += bytes([TS_LARGE_DELTA]) + (stamp - self._last_stamp).to_bytes(2, "little")
            self._last_stamp = stamp
            x, y, z = (0, 0, ONE_G) if sensor in ACCEL_IDS else (0, 0, 0)
            fifo += bytes([sensor]) + struct.pack("<hhh", x, y, z)
        if len(fifo) > FIFO_LIMIT:
            del fifo[:len(fifo) - FIFO_LIMIT]
            self._last_stamp = None    # the next batch must carry a full stamp

    def _interrupt_status(self) -> int:
        status = 0
        if self.fifos[REG_FIFO_W]:
            status |= INT_FIFO_W_DRDY
        if self.fifos[REG_FIFO_NW]:
            status |= INT_FIFO_NW_DRDY
        if self.status:
            status |= INT_STATUS_READY
        return status | (1 if status else 0)

    def _fifo_read(self, reg: int, count: int) -> bytes:
        pending = self._transfer.get(reg)
        if not pending:
            self._produce()
            fifo = self.fifos[reg]
            pending = bytearray(struct.pack("<H", len(fifo))) + fifo
            fifo.clear()
            self._transfer[reg] = pending
        out = bytes(pending[:count])
        del pending[:count]
        return out.ljust(count, b"\x00")

    def _status_read(self, count: int) -> bytes:
        pending = self._transfer.get(REG_STATUS)
        if not pending:
            pending = bytearray(self.status.pop(0) if self.status else b"\x00\x00\x00\x00")
            self._transfer[REG_STATUS] = pending
        out = bytes(pending[:count])
        del pending[:count]
        return out.ljust(count, b"\x00")

    def summary(self) -> str:
        sensors = ", ".join(f"{s}@{r:g}Hz" for s, r in sorted(self.enabled.items())) or "none"
        return (f"BHI260AP: {self.uploaded} byte(s) of firmware, booted {self.booted}, "
                f"sensors {sensors}"
                + (f", unmodelled {sorted(set(self.unmodelled))}" if self.unmodelled else ""))
