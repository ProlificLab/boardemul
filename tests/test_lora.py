#!/usr/bin/env python3
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
    """Where it was sent, when, and for how long: enough to know whether it was
    on this channel and whether anything else was in the air with it."""
    frame = lora.encode(906875000, 1234.5, 0.25, b"payload")
    frequency, sent_at, airtime, payload = lora.decode(frame)
    assert frequency == 906875000
    assert sent_at == pytest.approx(1234.5)
    assert airtime == pytest.approx(0.25)
    assert payload == b"payload"


def test_something_else_on_the_air_is_not_mistaken_for_a_packet() -> None:
    assert lora.decode(b"not ours at all") is None
    assert lora.decode(b"") is None
