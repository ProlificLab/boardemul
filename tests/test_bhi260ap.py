#!/usr/bin/env python3
"""The BHI260AP sensor hub's host interface (ADR 0108), as Bosch's driver
speaks it: reset, upload, boot, parameters, sensor configuration, FIFOs."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices import bhi260ap as hub  # noqa: E402


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _write(dev, reg: int, data: bytes) -> None:
    dev.start(reading=False)
    dev.write(bytes([reg]) + data)


def _read(dev, reg: int, count: int) -> bytes:
    dev.start(reading=False)
    dev.write(bytes([reg]))
    dev.start(reading=True)
    return dev.read(count)


def _command(dev, cmd: int, payload: bytes = b"", words: bool = False) -> None:
    length = len(payload) // 4 if words else len(payload)
    padded = payload + bytes(-len(payload) % 4)
    _write(dev, hub.REG_CHAN_CMD, struct.pack("<HH", cmd, length) + padded)


def _booted(clock=None):
    dev = hub.BHI260AP(clock=clock)
    _write(dev, hub.REG_RESET_REQ, b"\x01")
    _command(dev, hub.CMD_UPLOAD_TO_PROGRAM_RAM, bytes(64), words=True)
    _command(dev, hub.CMD_BOOT_PROGRAM_RAM)
    return dev


def test_it_identifies_itself() -> None:
    dev = hub.BHI260AP()
    assert _read(dev, hub.REG_PRODUCT_ID, 1)[0] == 0x89
    assert _read(dev, hub.REG_CHIP_ID, 1)[0] == 0x70


def test_a_reset_leaves_the_host_interface_ready_and_no_firmware() -> None:
    dev = hub.BHI260AP()
    _write(dev, hub.REG_RESET_REQ, b"\x01")
    assert _read(dev, hub.REG_BOOT_STATUS, 1)[0] == hub.BOOT_HOST_INTERFACE_READY
    assert _read(dev, hub.REG_KERNEL_VERSION, 2) == b"\x00\x00"


def test_an_upload_split_across_writes_boots() -> None:
    dev = hub.BHI260AP()
    image = bytes(range(256)) * 2
    first = struct.pack("<HH", hub.CMD_UPLOAD_TO_PROGRAM_RAM, len(image) // 4) + image[:100]
    _write(dev, hub.REG_CHAN_CMD, first)
    _write(dev, hub.REG_CHAN_CMD, image[100:])
    assert dev.uploaded == len(image)
    _command(dev, hub.CMD_BOOT_PROGRAM_RAM)
    boot = _read(dev, hub.REG_BOOT_STATUS, 1)[0]
    assert boot & hub.BOOT_HOST_INTERFACE_READY and boot & hub.BOOT_FW_VERIFY_DONE
    assert _read(dev, hub.REG_KERNEL_VERSION, 2) != b"\x00\x00"


def test_a_parameter_is_answered_on_the_status_channel() -> None:
    dev = _booted()
    _command(dev, hub.PARAM_READ | hub.PARAM_VIRT_SENSOR_PRESENT)
    assert _read(dev, hub.REG_INT_STATUS, 1)[0] & hub.INT_STATUS_READY
    code, length = struct.unpack("<HH", _read(dev, hub.REG_STATUS, 4))
    assert (code, length) == (hub.PARAM_VIRT_SENSOR_PRESENT, 32)
    present = _read(dev, hub.REG_STATUS, 32)
    assert present[0] & (1 << 1), "the accelerometer's pass-through is present"
    assert present[1] & (1 << 2), "and the gyroscope's (id 10)"


def test_sensor_info_gives_the_event_size_at_offset_20() -> None:
    dev = _booted()
    _command(dev, hub.PARAM_READ | (hub.PARAM_SENSOR_INFO_0 + 1))
    code, length = struct.unpack("<HH", _read(dev, hub.REG_STATUS, 4))
    info = _read(dev, hub.REG_STATUS, length)
    assert code == hub.PARAM_SENSOR_INFO_0 + 1 and length == 28
    assert info[20] == hub.EVENT_SIZE


def test_a_configured_sensor_streams_on_the_board_clock() -> None:
    clock = _Clock()
    dev = _booted(clock)
    _command(dev, hub.CMD_CONFIG_SENSOR, bytes([1]) + struct.pack("<f", 100.0) + bytes(3))
    clock.now = 0.0
    assert _read(dev, hub.REG_INT_STATUS, 1)[0] & hub.INT_FIFO_NW_DRDY
    clock.now = 0.05
    length = struct.unpack("<H", _read(dev, hub.REG_FIFO_NW, 2))[0]
    data = _read(dev, hub.REG_FIFO_NW, length)
    assert data[0] == hub.TS_FULL
    samples = [i for i in range(len(data)) if data[i] == 1]
    first = data.index(1, 6)
    assert struct.unpack_from("<hhh", data, first + 1) == (0, 0, hub.ONE_G)
    assert 5 <= len(samples) <= 7, "100 Hz for 50 ms is about five samples"


def test_a_fifo_transfer_starts_with_its_length_and_does_not_advance() -> None:
    dev = _booted(_Clock())
    assert _read(dev, hub.REG_FIFO_W, 2) == b"\x00\x00"
