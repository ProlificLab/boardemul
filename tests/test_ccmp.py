#!/usr/bin/env python3
"""The 802.11 framing around AES-CCM.

The cipher has its own tests (`test_aes`); these check the wrapping that 802.11
adds and that is easy to get subtly wrong: the packet number split across the
header, the nonce, and above all the additional-authenticated-data, which must
mask exactly the header bits that change in flight -- no more, or a retransmit
fails to verify; no fewer, or a forgery passes. The proof of the masking is a
frame sealed one way and verified against a header with those bits flipped.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import ccmp  # noqa: E402

TK = bytes(range(16))
AP = bytes.fromhex("020000000001")
STA = bytes.fromhex("0254444d0001")
OTHER = bytes.fromhex("0254444d0002")
BODY = bytes.fromhex("aaaa03000000") + b"\x08\x00" + b"a plaintext MSDU body"


def _hdr(fc0: int = 0x08, fc1: int = 0x42, a1: bytes = STA, a2: bytes = AP,
         a3: bytes = AP, seq: int = 0, qos: int | None = None) -> bytes:
    h = (bytes([fc0, fc1, 0, 0]) + a1 + a2 + a3
         + struct.pack("<H", (seq & 0xFFF) << 4))
    if qos is not None:
        h += struct.pack("<H", qos)
    return h


def test_the_packet_number_survives_the_header_split() -> None:
    pn = 0x0605_0403_0201
    assert ccmp.header_pn(ccmp.build_header(pn)) == pn
    assert ccmp.build_header(0)[3] == 0x20, "the Ext IV bit is set"


def test_the_nonce_is_flags_address_and_packet_number() -> None:
    n = ccmp.nonce(_hdr(a2=AP), pn=0x0102_0304_0506)
    assert n[0] == 0x00, "a non-QoS data frame has priority zero, not management"
    assert n[1:7] == AP, "the transmitter address"
    assert n[7:] == bytes.fromhex("010203040506"), "the packet number, big-endian"


def test_a_qos_frame_carries_its_tid_as_the_priority() -> None:
    n = ccmp.nonce(_hdr(fc0=0x88, qos=0x0006), pn=1)
    assert n[0] == 6, "the priority is the QoS TID"


def test_the_aad_masks_the_volatile_header_bits() -> None:
    a = ccmp.aad(_hdr(fc1=0x42 | 0x08, seq=0x123))    # Retry set, a sequence number
    assert a[0] == 0x08, "the data subtype bits are clear"
    assert a[1] == 0x42, "Retry masked out, Protected forced on"
    assert a[2:8] == STA and a[8:14] == AP and a[14:20] == AP, "the three addresses"
    assert a[20:22] == b"\x00\x00", "the sequence number is masked to its fragment"
    assert len(a) == 22, "no QoS control, no fourth address"


def test_a_qos_aad_appends_the_tid() -> None:
    a = ccmp.aad(_hdr(fc0=0x88, qos=0x0026))          # TID 6, other bits set
    assert len(a) == 24 and a[22:24] == b"\x06\x00", "only the TID is kept"


def test_a_fourth_address_frame_is_refused() -> None:
    with pytest.raises(ValueError):
        ccmp.aad(_hdr(fc1=0x03))                       # ToDS and FromDS: WDS


def test_encrypt_then_decrypt_returns_the_body() -> None:
    ch = ccmp.build_header(pn=7)
    sealed = ccmp.encrypt(TK, _hdr(), ch, BODY)
    assert len(sealed) == len(BODY) + ccmp.MIC_LEN, "ciphertext plus the MIC"
    assert sealed[:len(BODY)] != BODY, "the body is actually encrypted"
    assert ccmp.decrypt(TK, _hdr(), ch, sealed) == BODY


def test_decrypt_rejects_a_wrong_key_header_or_body() -> None:
    ch = ccmp.build_header(pn=7)
    hdr = _hdr()
    sealed = ccmp.encrypt(TK, hdr, ch, BODY)
    assert ccmp.decrypt(bytes(16), hdr, ch, sealed) is None, "the wrong key"
    assert ccmp.decrypt(TK, _hdr(a2=OTHER), ch, sealed) is None, "a changed address"
    assert ccmp.decrypt(TK, hdr, ccmp.build_header(pn=8), sealed) is None, "a changed PN"
    torn = bytearray(sealed)
    torn[0] ^= 1
    assert ccmp.decrypt(TK, hdr, ch, bytes(torn)) is None, "a changed body"


def test_the_masked_bits_are_not_authenticated() -> None:
    """The whole point of the masking: a frame sealed with Retry clear and
    Protected off must still verify against a header with Retry set and Protected
    on, because those bits are masked out of the AAD. Otherwise a retransmission,
    which sets Retry, would fail to decrypt."""
    ch = ccmp.build_header(pn=7)
    sealed = ccmp.encrypt(TK, _hdr(fc1=0x02), ch, BODY)      # From-DS only, no Protected
    seen = _hdr(fc1=0x02 | 0x40 | 0x08)                      # Protected and Retry set
    assert ccmp.decrypt(TK, seen, ch, sealed) == BODY
