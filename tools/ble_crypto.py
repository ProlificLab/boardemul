#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The cryptography a Bluetooth LE central needs, standard library only.

- AES-128 encryption (FIPS-197), the block cipher everything below is built on.
- AES-CMAC (RFC 4493), the MAC of LE Secure Connections' key functions.
- AES-CCM (RFC 3610) as the link layer uses it: a 13-octet nonce, a 4-octet
  MIC, two octets of length (Core Specification, Vol 6, Part E).
- P-256 (FIPS 186-4): key pairs and the Diffie-Hellman key of a pairing.
- The Security Manager's functions f4, f5, f6 and g2 (Vol 3, Part H, 2.2.6-9).

The Security Manager functions take and return values as they travel on the
air, least significant octet first, as Zephyr's and NimBLE's do; inside, the
specification's most-significant-first order is used and the conversion is
made here once. Slow next to C, and fast enough for one pairing.
"""

from __future__ import annotations

import secrets

# -- AES-128 ------------------------------------------------------------------

_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16")
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x11B) & 0xFF if a & 0x100 else a


def _expand(key: bytes) -> list[bytes]:
    words = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        w = list(words[i - 1])
        if i % 4 == 0:
            w = w[1:] + w[:1]
            w = [_SBOX[b] for b in w]
            w[0] ^= _RCON[i // 4 - 1]
        words.append([a ^ b for a, b in zip(words[i - 4], w)])
    return [bytes(sum(words[r * 4:r * 4 + 4], [])) for r in range(11)]


def aes_encrypt(key: bytes, block: bytes) -> bytes:
    """One AES-128 block, both arguments as the standard writes them."""
    if len(key) != 16 or len(block) != 16:
        raise ValueError("AES-128 takes a 16-octet key and block")
    rounds = _expand(key)
    s = [b ^ k for b, k in zip(block, rounds[0])]
    for r in range(1, 11):
        s = [_SBOX[b] for b in s]                                   # SubBytes
        s = [s[(i + 4 * (i % 4)) % 16] for i in range(16)]          # ShiftRows
        if r != 10:                                                 # MixColumns
            mixed = []
            for c in range(4):
                a = s[c * 4:c * 4 + 4]
                t = a[0] ^ a[1] ^ a[2] ^ a[3]
                mixed += [a[i] ^ t ^ _xtime(a[i] ^ a[(i + 1) % 4]) for i in range(4)]
            s = mixed
        s = [b ^ k for b, k in zip(s, rounds[r])]
    return bytes(s)


# -- AES-CMAC -----------------------------------------------------------------

def _shift1(block: bytes) -> bytes:
    n = int.from_bytes(block, "big") << 1
    return (n & ((1 << 128) - 1)).to_bytes(16, "big")


def aes_cmac(key: bytes, message: bytes) -> bytes:
    """RFC 4493."""
    l = aes_encrypt(key, bytes(16))
    k1 = _shift1(l)
    if l[0] & 0x80:
        k1 = k1[:15] + bytes([k1[15] ^ 0x87])
    k2 = _shift1(k1)
    if k1[0] & 0x80:
        k2 = k2[:15] + bytes([k2[15] ^ 0x87])
    blocks = max(1, (len(message) + 15) // 16)
    last = message[(blocks - 1) * 16:]
    if len(last) == 16:
        last = bytes(a ^ b for a, b in zip(last, k1))
    else:
        last = bytes(a ^ b for a, b in zip(last + b"\x80" + bytes(15 - len(last)), k2))
    x = bytes(16)
    for i in range(blocks - 1):
        x = aes_encrypt(key, bytes(a ^ b for a, b in zip(x, message[i * 16:i * 16 + 16])))
    return aes_encrypt(key, bytes(a ^ b for a, b in zip(x, last)))


# -- AES-CCM ------------------------------------------------------------------

def _ccm_mac(key: bytes, nonce: bytes, data: bytes, aad: bytes, mic_len: int) -> bytes:
    flags = (0x40 if aad else 0) | (((mic_len - 2) // 2) << 3) | 1      # L = 2
    b0 = bytes([flags]) + nonce + len(data).to_bytes(2, "big")
    x = aes_encrypt(key, b0)
    if aad:
        a = len(aad).to_bytes(2, "big") + aad
        a += bytes(-len(a) % 16)
        for i in range(0, len(a), 16):
            x = aes_encrypt(key, bytes(p ^ q for p, q in zip(x, a[i:i + 16])))
    padded = data + bytes(-len(data) % 16)
    for i in range(0, len(padded), 16):
        x = aes_encrypt(key, bytes(p ^ q for p, q in zip(x, padded[i:i + 16])))
    return x[:mic_len]


def _ccm_ctr(key: bytes, nonce: bytes, counter: int) -> bytes:
    return aes_encrypt(key, bytes([1]) + nonce + counter.to_bytes(2, "big"))


def ccm_encrypt(key: bytes, nonce: bytes, data: bytes, aad: bytes = b"",
                mic_len: int = 4) -> bytes:
    """The ciphertext followed by its MIC (RFC 3610, L = 2)."""
    if len(nonce) != 13:
        raise ValueError("a CCM nonce is 13 octets here")
    mac = _ccm_mac(key, nonce, data, aad, mic_len)
    stream = b"".join(_ccm_ctr(key, nonce, i + 1) for i in range((len(data) + 15) // 16))
    s0 = _ccm_ctr(key, nonce, 0)
    return (bytes(p ^ q for p, q in zip(data, stream))
            + bytes(p ^ q for p, q in zip(mac, s0)))


def ccm_decrypt(key: bytes, nonce: bytes, sealed: bytes, aad: bytes = b"",
                mic_len: int = 4) -> bytes | None:
    """The plaintext, or None if the MIC does not check."""
    body, mic = sealed[:-mic_len], sealed[-mic_len:]
    stream = b"".join(_ccm_ctr(key, nonce, i + 1) for i in range((len(body) + 15) // 16))
    data = bytes(p ^ q for p, q in zip(body, stream))
    s0 = _ccm_ctr(key, nonce, 0)
    expected = bytes(p ^ q for p, q in zip(_ccm_mac(key, nonce, data, aad, mic_len), s0))
    return data if secrets.compare_digest(expected, mic) else None


# -- P-256 --------------------------------------------------------------------

P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
A = P - 3
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
     0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)


def _add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    (x1, y1), (x2, y2) = p1, p2
    if x1 == x2 and (y1 + y2) % P == 0:
        return None
    if p1 == p2:
        m = (3 * x1 * x1 + A) * pow(2 * y1, -1, P) % P
    else:
        m = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (m * m - x1 - x2) % P
    return (x3, (m * (x1 - x3) - y1) % P)


def scalar_mult(k: int, point=G):
    result, addend = None, point
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        k >>= 1
    return result


def on_curve(point) -> bool:
    x, y = point
    return (y * y - (x * x * x + A * x + B)) % P == 0


def p256_keypair() -> tuple[int, tuple[int, int]]:
    private = secrets.randbelow(N - 1) + 1
    return private, scalar_mult(private)


def p256_dhkey(private: int, peer: tuple[int, int]) -> bytes:
    """The shared secret's X coordinate, least significant octet first."""
    if not on_curve(peer):
        raise ValueError("the peer's public key is not on P-256")
    return scalar_mult(private, peer)[0].to_bytes(32, "little")


def public_key_le(point: tuple[int, int]) -> bytes:
    """A public key as the Security Manager sends it: X then Y, each LSO first."""
    return point[0].to_bytes(32, "little") + point[1].to_bytes(32, "little")


def public_key_from_le(raw: bytes) -> tuple[int, int]:
    return (int.from_bytes(raw[:32], "little"), int.from_bytes(raw[32:64], "little"))


# -- the Security Manager's functions (little-endian in and out) ----------------

def _be(raw: bytes) -> bytes:
    return raw[::-1]


def f4(u: bytes, v: bytes, x: bytes, z: int) -> bytes:
    """Confirm values: AES-CMAC_X(U || V || Z)."""
    return _be(aes_cmac(_be(x), _be(u) + _be(v) + bytes([z])))


SALT = bytes.fromhex("6C888391AAF5A53860370BDB5A6083BE")


def f5(w: bytes, n1: bytes, n2: bytes, a1: bytes, a2: bytes) -> tuple[bytes, bytes]:
    """MacKey and LTK. a1, a2 are 7 octets: the address, then its type, as
    the air carries a typed address (type last when written LSO first)."""
    t = aes_cmac(SALT, _be(w))
    def one(counter: int) -> bytes:
        return _be(aes_cmac(t, bytes([counter]) + b"btle" + _be(n1) + _be(n2)
                            + _be(a1) + _be(a2) + (256).to_bytes(2, "big")))
    return one(0), one(1)


def f6(w: bytes, n1: bytes, n2: bytes, r: bytes, io_cap: bytes,
       a1: bytes, a2: bytes) -> bytes:
    """DHKey checks."""
    return _be(aes_cmac(_be(w), _be(n1) + _be(n2) + _be(r) + _be(io_cap)
                        + _be(a1) + _be(a2)))


def g2(u: bytes, v: bytes, x: bytes, y: bytes) -> int:
    """Numeric comparison's six digits."""
    value = aes_cmac(_be(x), _be(u) + _be(v) + _be(y))
    return int.from_bytes(value[-4:], "big") % 1000000


def typed_address(address: bytes, random: bool) -> bytes:
    """An address (6 octets, LSO first) with its type, for f5 and f6."""
    return address + bytes([1 if random else 0])
