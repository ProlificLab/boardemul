#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The cipher, against the bytes the standards publish.

ADR 0065 is explicit about how far each layer can be proved, and this file is
laid out the same way. The block cipher and the key wrap are pinned to vectors
out of FIPS-197 and RFC 3394 -- if those pass, those layers are right. CCM has
no vector here, deliberately rather than by omission: a vector typed from
memory would look like a proof and be a guess. What is checked instead is that
it round trips and that the integrity code actually depends on every input it
is supposed to depend on, which catches a field that is not being fed in.

The gap CCM leaves is a mistake symmetric between encryption and decryption,
and the station is the only oracle for that.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import aes  # noqa: E402


def test_the_block_cipher_matches_fips_197() -> None:
    """The two AES-128 vectors from the standard: Appendix B's worked example
    and Appendix C.1's."""
    for key, plain, cipher in [
        ("2b7e151628aed2a6abf7158809cf4f3c",
         "3243f6a8885a308d313198a2e0370734",
         "3925841d02dc09fbdc118597196a0b32"),
        ("000102030405060708090a0b0c0d0e0f",
         "00112233445566778899aabbccddeeff",
         "69c4e0d86a7b0430d8cdb78070b4c55a"),
    ]:
        got = aes.encrypt(bytes.fromhex(key), bytes.fromhex(plain))
        assert got.hex() == cipher, f"{key}/{plain}: {got.hex()} != {cipher}"


def test_the_substitution_box_is_a_permutation() -> None:
    """It is computed rather than typed in, so this says the computation is a
    bijection and hits the two entries every reference quotes. A box that is
    not a permutation is not invertible and AES would not be a cipher."""
    assert len(set(aes.SBOX)) == 256
    assert aes.SBOX[0x00] == 0x63
    assert aes.SBOX[0x53] == 0xED


def test_key_wrap_matches_rfc_3394() -> None:
    """Section 4.1: a hundred and twenty-eight bit key under a hundred and
    twenty-eight bit key encryption key."""
    got = aes.key_wrap(bytes.fromhex("000102030405060708090A0B0C0D0E0F"),
                       bytes.fromhex("00112233445566778899AABBCCDDEEFF"))
    assert got.hex().upper() == "1FA68B0A8112B447AEF34BD8FB5A7B829D3E862371D2CFE5"


def test_key_wrap_grows_by_one_block() -> None:
    """Wrapping adds the eight-byte check value and nothing else, which is
    what lets the caller size a key data field before wrapping it."""
    kek = bytes(range(16))
    for length in (16, 24, 32, 40):
        assert len(aes.key_wrap(kek, bytes(length))) == length + 8


def test_ccm_round_trips() -> None:
    """Encrypt then decrypt, over the shapes CCMP actually produces: a
    thirteen-byte nonce, an eight-byte integrity code, additional data that is
    not a whole number of blocks, and a payload that is not either."""
    key, nonce = bytes(range(16)), bytes(range(13))
    for aad_len in (0, 22, 24, 30):
        for body_len in (0, 1, 15, 16, 17, 100):
            aad, body = bytes(range(aad_len)), bytes(range(body_len % 256))[:body_len]
            sealed = aes.ccm_encrypt(key, nonce, aad, body)
            assert len(sealed) == body_len + 8
            assert aes.ccm_decrypt(key, nonce, aad, sealed) == body


def test_ccm_rejects_what_it_should() -> None:
    """The integrity code has to depend on every input. Changing any of them by
    one bit has to make the frame fail, or that input is not reaching the
    code -- which is the mistake a round trip on its own cannot see."""
    key, nonce, aad = bytes(range(16)), bytes(range(13)), bytes(range(22))
    body = b"the quick brown fox"
    sealed = aes.ccm_encrypt(key, nonce, aad, body)

    other = bytearray(key); other[0] ^= 1
    assert aes.ccm_decrypt(bytes(other), nonce, aad, sealed) is None, "the key"
    other = bytearray(nonce); other[0] ^= 1
    assert aes.ccm_decrypt(key, bytes(other), aad, sealed) is None, "the nonce"
    other = bytearray(aad); other[0] ^= 1
    assert aes.ccm_decrypt(key, nonce, bytes(other), sealed) is None, "the data"
    other = bytearray(sealed); other[0] ^= 1
    assert aes.ccm_decrypt(key, nonce, aad, bytes(other)) is None, "the body"
    other = bytearray(sealed); other[-1] ^= 1
    assert aes.ccm_decrypt(key, nonce, aad, bytes(other)) is None, "the code"
    assert aes.ccm_decrypt(key, nonce, aad, sealed[:4]) is None, "a short frame"


def test_ccm_is_not_a_fixed_keystream() -> None:
    """A counter block that ignored the counter would encrypt every block the
    same way, which round trips perfectly and is catastrophic. Two identical
    plaintext blocks must not come out identical."""
    sealed = aes.ccm_encrypt(bytes(range(16)), bytes(range(13)), b"", bytes(32))
    assert sealed[:16] != sealed[16:32], "the counter is not reaching the stream"
