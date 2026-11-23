#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Device model tests.

Every case here corresponds to a bug that actually happened. They are fast
because they touch no emulator: a device model is an ordinary object, and the
failures worth catching are all in how it answers a question.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.i2c_devices import (  # noqa: E402
    BQ27220, CST328, DRV2605, TCA8418, XL9555,
)


def address_report(touch: CST328) -> None:
    """Select the report register the way the driver does."""
    touch.start(reading=False)
    touch.write(bytes([0xD0, 0x00]))


# -- CST328 -----------------------------------------------------------------


@pytest.mark.parametrize("split", [1, 2, 3, 4, 5, 6])
def test_report_survives_a_split_read(split: int) -> None:
    """One logical read reaches the device as several calls.

    The controller splits it into n-1 acknowledged bytes plus a final
    unacknowledged one, and the driver may split it again across command lists.
    A model that rebuilds its reply per call hands out byte 0 where byte 6 was
    expected -- and the driver then discards the report while still
    acknowledging it, so nothing looks wrong on the bus.
    """
    touch = CST328()
    touch.touch = (120, 75)
    address_report(touch)

    touch.start(reading=True)
    first = touch.read(split)
    touch.start(reading=True)          # a repeated START mid-read
    rest = touch.read(7 - split)
    report = first + rest

    assert len(report) == 7
    assert report[6] == 0xAB, "validity magic must survive the split"
    assert report[0] != 0xAB
    assert report[5] & 0x7F == 1, "one finger"
    assert (report[1] << 4) | (report[3] >> 4) == 120
    assert (report[2] << 4) | (report[3] & 0x0F) == 75


def test_report_is_coherent_when_read_whole() -> None:
    touch = CST328()
    touch.touch = (239, 319)           # the far corner, both axes at maximum
    address_report(touch)
    touch.start(reading=True)
    report = touch.read(7)
    assert (report[1] << 4) | (report[3] >> 4) == 239
    assert (report[2] << 4) | (report[3] & 0x0F) == 319


def test_no_touch_reports_no_fingers() -> None:
    touch = CST328()
    address_report(touch)
    touch.start(reading=True)
    report = touch.read(7)
    assert report[6] == 0xAB, "a finger-up report is still a valid report"
    assert report[5] & 0x7F == 0


def test_cst66xx_probe_is_rejected() -> None:
    """The driver tries a cst66xx at this address first and only falls through
    to the CST328 path if bytes 2 and 3 are not both 0xCA."""
    touch = CST328()
    touch.start(reading=False)
    touch.write(bytes([0xD0, 0x03, 0x00, 0x00]))
    touch.start(reading=True)
    probe = touch.read(50)
    assert probe[2:4] != bytes([0xCA, 0xCA])


def test_acknowledgement_is_counted() -> None:
    touch = CST328()
    touch.start(reading=False)
    touch.write(bytes([0xD0, 0x00, 0xAB]))
    assert touch.acks == 1


# -- TCA8418 ----------------------------------------------------------------


# From examples/factory/peri_keypad.cpp. Columns are mirrored in hardware --
# the firmware recovers the column as (COLS-1) - k % COLS -- so getting this
# wrong yields 'p' where 'q' was pressed.
VENDOR_PRESS_BYTES = {
    "Q": 0x8A, "W": 0x89, "E": 0x88, "R": 0x87, "T": 0x86,
    "Y": 0x85, "U": 0x84, "I": 0x83, "O": 0x82, "P": 0x81,
    "A": 0x94, "S": 0x93, "D": 0x92, "F": 0x91, "G": 0x90,
    "H": 0x8F, "J": 0x8E, "K": 0x8D, "L": 0x8C, "DEL": 0x8B,
    "ALT": 0x9E, "Z": 0x9D, "X": 0x9C, "C": 0x9B, "V": 0x9A,
    "B": 0x99, "N": 0x98, "M": 0x97, "$": 0x96, "ENTER": 0x95,
    "UP": 0xA3, "0": 0xA2, "SPACE": 0xA1, "SYM": 0xA0, "UP2": 0x9F,
}


@pytest.mark.parametrize("key,expected", sorted(VENDOR_PRESS_BYTES.items()))
def test_key_encoding_matches_the_vendor_table(key: str, expected: int) -> None:
    keypad = TCA8418()
    assert keypad.key_event(key, down=True)
    assert keypad.pending[-1] == expected


def test_release_clears_the_press_bit() -> None:
    keypad = TCA8418()
    keypad.key_event("Q", down=True)
    keypad.key_event("Q", down=False)
    assert keypad.pending == [0x8A, 0x0A]


def test_unknown_key_is_reported_not_swallowed() -> None:
    keypad = TCA8418()
    assert keypad.key_event("F1", down=True) is False
    assert keypad.pending == []


def read_register(device, reg: int, count: int = 1) -> bytes:
    device.start(reading=False)
    device.write(bytes([reg]))
    device.start(reading=True)
    return device.read(count)


def test_empty_event_fifo_reads_zero() -> None:
    """The driver's init flushes with `while (getEvent() != 0)`. Anything but
    zero from an empty FIFO hangs the boot."""
    keypad = TCA8418()
    assert read_register(keypad, TCA8418.KEY_EVENT_A)[0] == 0x00


def test_event_count_falls_as_events_are_consumed() -> None:
    keypad = TCA8418()
    for key in ("Q", "W", "E"):
        keypad.key_event(key, down=True)
    assert read_register(keypad, TCA8418.KEY_LCK_EC)[0] & 0x0F == 3
    assert read_register(keypad, TCA8418.KEY_EVENT_A)[0] == 0x8A
    assert read_register(keypad, TCA8418.KEY_LCK_EC)[0] & 0x0F == 2


# -- register-file devices --------------------------------------------------


def test_register_pointer_auto_increments() -> None:
    codec = DRV2605()
    codec.start(reading=False)
    codec.write(bytes([0x10, 0xAA, 0xBB, 0xCC]))
    assert read_register(codec, 0x10, 3) == bytes([0xAA, 0xBB, 0xCC])


def test_expander_reads_back_what_it_drove() -> None:
    """Firmware that sets an output and reads the input port expects to see its
    own value; a board with nothing attached behaves that way."""
    expander = XL9555()
    expander.start(reading=False)
    expander.write(bytes([XL9555.OUT0, 0x5A]))
    assert read_register(expander, XL9555.IN0)[0] == 0x5A


def test_haptic_reports_a_device_id() -> None:
    """Bits 7:5 of the status register carry the id; zero reads as absent."""
    assert read_register(DRV2605(), 0x00)[0] >> 5 != 0


def _subcommand(gauge: BQ27220, register: int, word: int, data: bytes = b"") -> None:
    gauge.start(reading=False)
    gauge.write(bytes([register]) + word.to_bytes(2, "little") + data)


def _status(gauge: BQ27220) -> int:
    return int.from_bytes(read_register(gauge, BQ27220.OPERATION_STATUS, 2), "little")


def test_fuel_gauge_answers_its_device_number() -> None:
    """DEVICE_NUMBER written to Control() is answered in MACData, as the Max's
    factory demo checks before it trusts the gauge."""
    gauge = BQ27220()
    _subcommand(gauge, BQ27220.CONTROL, BQ27220.DEVICE_NUMBER)
    assert read_register(gauge, BQ27220.MAC_DATA, 2) == bytes([0x20, 0x02])


def test_fuel_gauge_starts_sealed_and_initialised() -> None:
    status = _status(BQ27220())
    assert (status >> 1) & 3 == BQ27220.SEC_SEALED
    assert status & BQ27220.INITCOMP


def test_fuel_gauge_unseals_only_with_both_keys_in_order() -> None:
    gauge = BQ27220()
    _subcommand(gauge, BQ27220.CONTROL, BQ27220.UNSEAL_KEY_2)
    _subcommand(gauge, BQ27220.CONTROL, BQ27220.UNSEAL_KEY_1)
    assert (_status(gauge) >> 1) & 3 == BQ27220.SEC_SEALED
    _subcommand(gauge, BQ27220.CONTROL, BQ27220.UNSEAL_KEY_2)
    assert (_status(gauge) >> 1) & 3 == BQ27220.SEC_UNSEALED


def test_fuel_gauge_data_memory_round_trip() -> None:
    """Enter configuration, write a parameter with its checksum, read it back."""
    gauge = BQ27220()
    _subcommand(gauge, BQ27220.CONTROL, BQ27220.UNSEAL_KEY_1)
    _subcommand(gauge, BQ27220.CONTROL, BQ27220.UNSEAL_KEY_2)
    _subcommand(gauge, BQ27220.MAC_SUBCMD, BQ27220.ENTER_CFG_UPDATE)
    assert _status(gauge) & BQ27220.CFGUPDATE

    address, value = 0x929F, (3000).to_bytes(2, "big")
    _subcommand(gauge, BQ27220.MAC_SUBCMD, address, value)
    checksum = 0xFF - (sum(address.to_bytes(2, "little") + value) & 0xFF)
    gauge.start(reading=False)
    gauge.write(bytes([BQ27220.MAC_DATA_SUM, checksum, len(value) + 4]))

    _subcommand(gauge, BQ27220.CONTROL, BQ27220.EXIT_CFG_UPDATE_REINIT)
    assert not _status(gauge) & BQ27220.CFGUPDATE
    _subcommand(gauge, BQ27220.MAC_SUBCMD, address)
    assert read_register(gauge, BQ27220.MAC_DATA, 2) == value


def test_fuel_gauge_refuses_a_write_with_a_bad_checksum() -> None:
    gauge = BQ27220()
    _subcommand(gauge, BQ27220.CONTROL, BQ27220.UNSEAL_KEY_1)
    _subcommand(gauge, BQ27220.CONTROL, BQ27220.UNSEAL_KEY_2)
    _subcommand(gauge, BQ27220.MAC_SUBCMD, BQ27220.ENTER_CFG_UPDATE)
    _subcommand(gauge, BQ27220.MAC_SUBCMD, 0x929F, bytes([0x0B, 0xB8]))
    gauge.start(reading=False)
    gauge.write(bytes([BQ27220.MAC_DATA_SUM, 0x00, 6]))
    _subcommand(gauge, BQ27220.MAC_SUBCMD, 0x929F)
    assert read_register(gauge, BQ27220.MAC_DATA, 2) == bytes(2)


def test_fuel_gauge_control_reads_back_its_status_not_the_last_command() -> None:
    gauge = BQ27220()
    _subcommand(gauge, BQ27220.CONTROL, BQ27220.UNSEAL_KEY_1)
    assert read_register(gauge, BQ27220.CONTROL, 2) == bytes(2)
