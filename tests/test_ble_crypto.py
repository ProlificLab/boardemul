#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""tools/ble_crypto.py against published vectors: FIPS-197's AES example,
RFC 4493's CMAC examples, RFC 3610's first CCM packet, and the Bluetooth Core
Specification's sample data for f4, f5, f6 and g2 (Vol 3, Part H, Appendix D)
as Zephyr's self-test carries them, least significant octet first."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import ble_crypto as c  # noqa: E402


def h(text: str) -> bytes:
    return bytes.fromhex(text.replace(" ", ""))


def test_aes_matches_fips_197() -> None:
    key = h("000102030405060708090a0b0c0d0e0f")
    assert c.aes_encrypt(key, h("00112233445566778899aabbccddeeff")) == \
        h("69c4e0d86a7b0430d8cdb78070b4c55a")


def test_cmac_matches_rfc_4493() -> None:
    key = h("2b7e151628aed2a6abf7158809cf4f3c")
    assert c.aes_cmac(key, b"") == h("bb1d6929e95937287fa37d129b756746")
    assert c.aes_cmac(key, h("6bc1bee22e409f96e93d7e117393172a")) == \
        h("070a16b46b4d4144f79bdd9dd04a287c")


def test_ccm_matches_rfc_3610_packet_one() -> None:
    key = h("C0C1C2C3C4C5C6C7C8C9CACBCCCDCECF")
    nonce = h("00000003020100A0A1A2A3A4A5")
    packet = bytes(range(31))
    sealed = c.ccm_encrypt(key, nonce, packet[8:], aad=packet[:8], mic_len=8)
    assert sealed == h("588C979A61C663D2F066D0C2C0F989806D5F6B61DAC38417E8D12CFDF926E0")
    assert c.ccm_decrypt(key, nonce, sealed, aad=packet[:8], mic_len=8) == packet[8:]
    tampered = sealed[:-1] + bytes([sealed[-1] ^ 1])
    assert c.ccm_decrypt(key, nonce, tampered, aad=packet[:8], mic_len=8) is None


U = h("e69d350e480103ccdbfdf4ac1191f4efb9a5f9e9a7832c5e2cbe97f2d203b020")
V = h("fdc57ff449dd4f6bfb7c9df1c29acb592ae7d4eefbfc0a909abbf6323d8b1855")
X = h("abae2b71ecb2ffff3e7377d15484cbd5")
Y = h("cfc43dfff78365216e5fa725cce7e8a6")
A1 = c.typed_address(h("cebf37371256"), random=False)
A2 = c.typed_address(h("c1cf2d7013a7"), random=False)


def test_f4_matches_the_specification() -> None:
    assert c.f4(U, V, X, 0) == h("2d8774a9bea1edf11cbda907f116c9f2")


def test_f5_matches_the_specification() -> None:
    w = h("98a6bf73f3348d86f166f8b4136b79999b7d390aa610103405adc857a33402ec")
    mackey, ltk = c.f5(w, X, Y, A1, A2)
    assert mackey == h("206e63ce206a3ffd024a08a176f16529")
    assert ltk == h("380a7594b52205982 3cdd769117986 69".replace(" ", ""))


def test_f6_matches_the_specification() -> None:
    w = h("206e63ce206a3ffd024a08a176f16529")
    r = h("c80f2d0cd242da0854bb53b43b34a312")
    assert c.f6(w, X, Y, r, h("020101"), A1, A2) == h("618f95da090b6cd2c5e8d09c9873c4e3")


def test_g2_matches_the_specification() -> None:
    assert c.g2(U, V, X, Y) == 0x2f9ed5ba % 1000000


def test_p256_diffie_hellman_agrees_both_ways() -> None:
    assert c.on_curve(c.G)
    a, pa = c.p256_keypair()
    b, pb = c.p256_keypair()
    assert c.on_curve(pa) and c.on_curve(pb)
    assert c.p256_dhkey(a, pb) == c.p256_dhkey(b, pa)
    assert c.public_key_from_le(c.public_key_le(pa)) == pa
