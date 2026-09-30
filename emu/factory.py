#!/usr/bin/env python3
"""The factory-test report: what the vendor firmware tells lilygo.local:8896.

Once it has an address and the time, the vendor unit-test firmware connects to
`lilygo.local:8896` and sends one JSON object, pretty-printed by cJSON, naming
itself -- then prints `Send mac address success!` and deletes the task that sent
it (ADR 0096). It reads nothing back. This is the server's half: turn the bytes
into a report once they make one.

Measured on the wire, from the image `T-Deck_UnitTest_250620`:

    {"SKU": "NA", "product": "T-Deck", "mac": "02:54:44:4D:00:01",
     "date": "2026-10-01,00:21"}

The date is the device's local time from NTP (the image names `CST-8`), as
`%Y-%m-%d,%H:%M`. The MAC is its factory MAC in upper-case hex.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

PORT = 8896


@dataclass
class Report:
    """One device's registration. A field the device left out, or sent as
    something other than a string, is None; the whole object is kept as sent."""
    sku: str | None
    product: str | None
    mac: str | None
    date: str | None
    fields: dict = field(default_factory=dict)


def parse_report(data: bytes) -> Report | None:
    """The report, once `data` holds a whole JSON object; None until then.

    The stream may arrive in pieces, so an incomplete object is not an error --
    it is not a report yet. Anything that is not a JSON object once complete is
    not a report either. Only the first object is taken: this device sends one.
    """
    try:
        text = data.decode("utf-8").lstrip()
    except UnicodeDecodeError:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    def text_of(key: str) -> str | None:
        value = obj.get(key)
        return value if isinstance(value, str) else None

    return Report(sku=text_of("SKU"), product=text_of("product"),
                  mac=text_of("mac"), date=text_of("date"), fields=obj)


def mac_bytes(text: str | None) -> bytes | None:
    """`02:54:44:4D:00:01` as six octets, or None if it is not a MAC."""
    if not text:
        return None
    parts = text.split(":")
    if len(parts) != 6:
        return None
    try:
        octets = bytes(int(p, 16) for p in parts)
    except ValueError:
        return None
    return octets if all(len(p) == 2 for p in parts) else None
