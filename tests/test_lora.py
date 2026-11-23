#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Time on air (ADR 0037).

The numbers are not invented and not tuned to make anything pass: they come
out of the datasheet's arithmetic applied to the settings a firmware programs,
and they agree with what a published calculator gives for the same settings.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices import lora  # noqa: E402

BW_125K, BW_250K, BW_500K = 0x04, 0x05, 0x06


def test_the_bandwidth_codes_are_the_parts_own() -> None:
    assert lora.bandwidth_hz(BW_125K) == 125000
    assert lora.bandwidth_hz(BW_250K) == 250000
    assert lora.bandwidth_hz(BW_500K) == 500000


@pytest.mark.parametrize("payload, spreading, bandwidth, expected_ms", [
    (20, 7, BW_125K, 57),       # the textbook case, and about 56 ms in practice
    (10, 7, BW_125K, 41),
    (20, 9, BW_125K, 185),
    (20, 12, BW_125K, 1319),      # worked through by hand from the formula
])
def test_it_agrees_with_the_published_figures(payload, spreading, bandwidth,
                                              expected_ms) -> None:
    """Within a couple of milliseconds of what a LoRa airtime calculator gives
    for the same settings, which is the check that this is arithmetic and not
    a plausible-looking curve."""
    got = lora.time_on_air(payload, spreading, bandwidth, 1) * 1000
    assert abs(got - expected_ms) <= 3, f"{got:.1f} ms, expected about {expected_ms}"


def test_a_slower_spreading_factor_takes_longer() -> None:
    faster = lora.time_on_air(50, 7, BW_125K, 1)
    slower = lora.time_on_air(50, 12, BW_125K, 1)
    assert slower > faster * 10


def test_a_wider_channel_is_quicker() -> None:
    narrow = lora.time_on_air(50, 9, BW_125K, 1)
    wide = lora.time_on_air(50, 9, BW_500K, 1)
    assert wide == pytest.approx(narrow / 4, rel=0.01)


def test_a_longer_packet_takes_longer() -> None:
    assert lora.time_on_air(100, 9, BW_250K, 1) > lora.time_on_air(10, 9, BW_250K, 1)


def test_a_frame_carries_what_a_receiver_needs_to_judge_it() -> None:
    """Which channel, when, for how long, how hard it was pushed and from
    where: enough to work out whether this receiver could have decoded it."""
    frame = lora.encode(906875000, 1234.5, 0.25, b"payload",
                        power_dbm=17.0, x=100.0, y=-50.0)
    got = lora.decode(frame)
    assert got.frequency == 906875000
    assert got.sent_at == pytest.approx(1234.5)
    assert got.airtime == pytest.approx(0.25)
    assert got.power_dbm == pytest.approx(17.0)
    assert got.origin == pytest.approx((100.0, -50.0))
    assert got.payload == b"payload"


def test_something_else_on_the_air_is_not_mistaken_for_a_packet() -> None:
    assert lora.decode(b"not ours at all") is None
    assert lora.decode(b"") is None


# -- the link a packet has to cross -----------------------------------------

@pytest.mark.parametrize("spreading, datasheet_dbm", [
    (7, -124), (8, -127), (9, -129), (10, -132), (11, -134.5), (12, -137),
])
def test_sensitivity_matches_the_datasheet(spreading, datasheet_dbm) -> None:
    """Derived from the noise floor and what each spreading factor can dig out
    of it, and it lands on the figures the part's own datasheet quotes. That
    agreement is the evidence these are not invented numbers (ADR 0038)."""
    got = lora.sensitivity_dbm(BW_125K, spreading)
    assert abs(got - datasheet_dbm) <= 1.0, f"{got:.1f} dBm, datasheet {datasheet_dbm}"


def test_a_slower_spreading_factor_hears_a_weaker_signal() -> None:
    assert lora.sensitivity_dbm(BW_125K, 12) < lora.sensitivity_dbm(BW_125K, 7)


def test_a_wider_channel_takes_in_more_noise() -> None:
    assert lora.noise_floor_dbm(BW_500K) > lora.noise_floor_dbm(BW_125K)


def test_the_frequency_register_is_not_hertz() -> None:
    """The part counts in steps of its crystal, and a propagation sum given the
    raw value would be using the wrong frequency."""
    raw = round(906_875_000 / lora.FREQUENCY_STEP_HZ)
    assert lora.register_to_hz(raw) == pytest.approx(906_875_000, abs=1)
    assert raw != 906_875_000


def test_loss_grows_with_distance() -> None:
    near = lora.path_loss_db(100, 906_875_000)
    far = lora.path_loss_db(1000, 906_875_000)
    assert far == pytest.approx(near + 20, abs=0.1), "ten times as far is 20 dB"


# -- the place between the boards -------------------------------------------

def test_the_default_is_exactly_free_space() -> None:
    """The log-distance model reduces to free space at an exponent of two, so
    a scenario that says nothing about its surroundings behaves as before."""
    for metres in (1, 10, 1000, 100000):
        assert lora.path_loss_db(metres, 906_875_000) == pytest.approx(
            lora.free_space_loss_db(metres, 906_875_000), abs=1e-9)


def test_a_more_obstructed_place_costs_more() -> None:
    at_a_km = {name: lora.path_loss_db(1000, 906_875_000, n)
               for name, n in lora.ENVIRONMENTS.items()}
    assert (at_a_km["free-space"] < at_a_km["open"] < at_a_km["suburban"]
            < at_a_km["urban"] < at_a_km["indoor"])


def test_the_exponent_is_what_it_says_it_is() -> None:
    """Ten times the distance costs ten times the exponent in decibels, which
    is the whole content of the model."""
    near = lora.path_loss_db(100, 906_875_000, 3.0)
    far = lora.path_loss_db(1000, 906_875_000, 3.0)
    assert far - near == pytest.approx(30.0, abs=0.01)


def test_an_aerial_helps_at_both_ends() -> None:
    plain = lora.received_dbm(22, 1000, 906_875_000)
    better = lora.received_dbm(22, 1000, 906_875_000,
                               transmit_gain_dbi=3.0, receive_gain_dbi=2.0)
    assert better - plain == pytest.approx(5.0, abs=0.01)


def test_something_in_the_way_is_subtracted() -> None:
    clear = lora.received_dbm(22, 1000, 906_875_000)
    blocked = lora.received_dbm(22, 1000, 906_875_000, shadowing_db=12.0)
    assert clear - blocked == pytest.approx(12.0, abs=0.01)


def test_a_town_shortens_the_link() -> None:
    """The point of all of it: the same two boards that reach each other across
    open ground do not through a built-up area."""
    floor = lora.sensitivity_dbm(BW_250K, 11)
    assert lora.received_dbm(22, 20_000, 906_875_000,
                             lora.ENVIRONMENTS["free-space"]) > floor
    assert lora.received_dbm(22, 20_000, 906_875_000,
                             lora.ENVIRONMENTS["urban"]) < floor


def test_two_boards_at_the_same_spot_lose_nothing_much() -> None:
    """Which is where every board stands unless a scenario says otherwise, and
    is why giving no positions leaves everything audible."""
    assert lora.received_dbm(22, 0, 906_875_000) > lora.sensitivity_dbm(BW_250K, 11)


def test_far_enough_apart_is_out_of_range() -> None:
    arrived = lora.received_dbm(22, 3_000_000, 906_875_000)
    assert arrived < lora.sensitivity_dbm(BW_250K, 11)
