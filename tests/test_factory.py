#!/usr/bin/env python3
"""The factory-test report parser.

Checked against the exact bytes the vendor firmware sent on the wire (ADR 0096)
-- cJSON's pretty print, tabs and newlines -- and against the ways a stream can
fail to be a report yet: cut short, not JSON, not an object.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import factory  # noqa: E402

# As captured from T-Deck_UnitTest_250620 on port 8896.
WIRE = (b'{\n\t"SKU":\t"NA",\n\t"product":\t"T-Deck",\n\t"mac":\t'
        b'"02:54:44:4D:00:01",\n\t"date":\t"2026-10-01,00:21"\n}')


def test_the_report_on_the_wire_parses() -> None:
    report = factory.parse_report(WIRE)
    assert report is not None
    assert (report.sku, report.product, report.mac, report.date) == (
        "NA", "T-Deck", "02:54:44:4D:00:01", "2026-10-01,00:21")


def test_a_report_cut_short_is_not_a_report_yet() -> None:
    """It may arrive in pieces; every prefix short of the whole is 'not yet'."""
    for cut in range(len(WIRE)):
        assert factory.parse_report(WIRE[:cut]) is None, f"cut at {cut}"


def test_what_is_not_a_json_object_is_not_a_report() -> None:
    assert factory.parse_report(b"GET / HTTP/1.1\r\n\r\n") is None
    assert factory.parse_report(b"[1, 2, 3]") is None
    assert factory.parse_report(b"\xff\xfe") is None


def test_a_missing_or_odd_field_is_none_and_the_rest_is_kept() -> None:
    report = factory.parse_report(b'{"product": "T-Deck", "mac": 5, "x": 1}')
    assert report is not None
    assert report.product == "T-Deck" and report.mac is None and report.sku is None
    assert report.fields["x"] == 1, "the object is kept as sent"


def test_the_mac_reads_as_six_octets() -> None:
    assert factory.mac_bytes("02:54:44:4D:00:01") == bytes.fromhex("0254444d0001")
    assert factory.mac_bytes("02:54:44:4D:00") is None
    assert factory.mac_bytes("02:54:44:4D:00:ZZ") is None
    assert factory.mac_bytes(None) is None
