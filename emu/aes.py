#!/usr/bin/env python3
"""AES-128, key wrap and CCM, written out because no standard library has them.

Narrow on purpose (ADR 0065): one key length, encryption only, and the two
constructions WPA2 needs on top of it. AES key wrap needs the block cipher
forwards to wrap and only the station ever unwraps; CCM is counter mode with a
CBC integrity code and uses the cipher forwards in both directions. So there is
no decryption here, and nothing here is unreachable.

Nothing in this file knows what a frame is. The 802.11 bookkeeping -- which
bits of which header field go into the additional data, how the nonce is
assembled -- lives with the peer that has a header to read it from.
"""

from __future__ import annotations

BLOCK = 16
KEY_BYTES = 16
ROUNDS = 10


def _xtime(a: int) -> int:
    """Multiply by x in GF(2^8), modulo the AES polynomial x^8+x^4+x^3+x+1."""
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _mul(a: int, b: int) -> int:
    """Multiply in GF(2^8): shift and add, for eight bits of b."""
    out = 0
    for _ in range(8):
        if b & 1:
            out ^= a
        b >>= 1
        a = _xtime(a)
    return out


def _build_sbox() -> bytes:
    """The substitution box, computed rather than typed in.

    Each byte maps to its multiplicative inverse in GF(2^8) -- zero to itself
    -- and then through the affine map the standard specifies. Two hundred and
    fifty-six literals would be two hundred and fifty-six chances to typo one
    byte and still decrypt almost everything, which is the sort of bug that
    survives a test suite.

    The inverses come from a log table built by walking the powers of 3, which
    generates the multiplicative group.
    """
    powers = [0] * 256          # powers[i] = 3**i
    logs = [0] * 256            # logs[3**i] = i
    x = 1
    for i in range(255):
        powers[i] = x
        logs[x] = i
        x ^= _xtime(x)          # multiply by 3 == multiply by (x + 1)
    box = bytearray(256)
    for value in range(256):
        # Modulo 255 because the exponents wrap there: the inverse of 1 is 1,
        # and 255 - logs[1] is 255, which is one past the end of the table.
        inverse = 0 if value == 0 else powers[(255 - logs[value]) % 255]
        # The affine map: y = inverse ^ rotl(inverse,1) ^ ... ^ rotl(inverse,4) ^ 0x63
        out = inverse
        for shift in (1, 2, 3, 4):
            out ^= ((inverse << shift) | (inverse >> (8 - shift))) & 0xFF
        box[value] = out ^ 0x63
    return bytes(box)


SBOX = _build_sbox()
# The two entries every reference states, so a build of the table above that
# went wrong says so at import rather than at the far end of a handshake.
assert SBOX[0x00] == 0x63 and SBOX[0x53] == 0xED, "the substitution box is wrong"
assert len(set(SBOX)) == 256, "the substitution box is not a permutation"

def _build_rcon() -> tuple[int, ...]:
    """The round constants: successive powers of x in GF(2^8), so the eighth is
    0x1b rather than 0x100. Computed for the same reason as the box above."""
    out, value = [], 1
    for _ in range(ROUNDS):
        out.append(value)
        value = _xtime(value)
    return tuple(out)


RCON = _build_rcon()
assert RCON[:4] == (0x01, 0x02, 0x04, 0x08) and RCON[8] == 0x1B, "round constants"


def expand_key(key: bytes) -> list[list[int]]:
    """The eleven round keys, four bytes at a time.

    Every fourth word is rotated, substituted and xored with a round constant;
    the rest are the previous word xored with the one four back.
    """
    if len(key) != KEY_BYTES:
        raise ValueError(f"AES-128 only: a {len(key)}-byte key was given")
    words = [list(key[i:i + 4]) for i in range(0, KEY_BYTES, 4)]
    for i in range(4, 4 * (ROUNDS + 1)):
        word = list(words[i - 1])
        if i % 4 == 0:
            word = word[1:] + word[:1]                      # RotWord
            word = [SBOX[b] for b in word]                  # SubWord
            word[0] ^= RCON[i // 4 - 1]
        words.append([a ^ b for a, b in zip(words[i - 4], word)])
    return [sum(words[r * 4:(r + 1) * 4], []) for r in range(ROUNDS + 1)]


def encrypt_block(round_keys: list[list[int]], block: bytes) -> bytes:
    """One sixteen-byte block, forwards.

    The state is column-major, which is why ShiftRows below indexes in steps of
    four: byte 4*c+r is row r of column c.
    """
    state = [b ^ k for b, k in zip(block, round_keys[0])]
    for rnd in range(1, ROUNDS + 1):
        state = [SBOX[b] for b in state]
        # ShiftRows: row r rotates left by r.
        state = [state[(i + 4 * (i % 4)) % 16] for i in range(16)]
        if rnd != ROUNDS:
            # MixColumns: each column times the fixed matrix, per column.
            mixed = []
            for c in range(0, 16, 4):
                a0, a1, a2, a3 = state[c:c + 4]
                mixed += [
                    _xtime(a0) ^ _xtime(a1) ^ a1 ^ a2 ^ a3,
                    a0 ^ _xtime(a1) ^ _xtime(a2) ^ a2 ^ a3,
                    a0 ^ a1 ^ _xtime(a2) ^ _xtime(a3) ^ a3,
                    _xtime(a0) ^ a0 ^ a1 ^ a2 ^ _xtime(a3),
                ]
            state = mixed
        state = [b ^ k for b, k in zip(state, round_keys[rnd])]
    return bytes(state)


def encrypt(key: bytes, block: bytes) -> bytes:
    """One block under one key, for a caller with only one block to do."""
    if len(block) != BLOCK:
        raise ValueError(f"a block is {BLOCK} bytes, not {len(block)}")
    return encrypt_block(expand_key(key), block)


# ---------------------------------------------------------------- key wrap

WRAP_IV = b"\xa6\xa6\xa6\xa6\xa6\xa6\xa6\xa6"


def key_wrap(kek: bytes, plain: bytes) -> bytes:
    """RFC 3394. Used once per handshake, on the group key.

    Six passes over the blocks, each mixing in a counter that runs from one to
    six times the block count, so a block's position matters as well as its
    contents.
    """
    if len(plain) % 8 or len(plain) < 16:
        raise ValueError("key wrap takes whole eight-byte blocks, at least two")
    schedule = expand_key(kek)
    blocks = [plain[i:i + 8] for i in range(0, len(plain), 8)]
    a = WRAP_IV
    for step in range(6):
        for i, block in enumerate(blocks):
            out = encrypt_block(schedule, a + block)
            counter = step * len(blocks) + i + 1
            a = bytes(x ^ y for x, y in
                      zip(out[:8], counter.to_bytes(8, "big")))
            blocks[i] = out[8:]
    return a + b"".join(blocks)


# --------------------------------------------------------------------- CCM

def _ccm_blocks(nonce: bytes, aad: bytes, length: int, mic_len: int,
                size_len: int) -> list[bytes]:
    """The blocks the integrity code is taken over: a flags block, then the
    additional data with its length in front, each padded to a block."""
    flags = (0x40 if aad else 0) | ((mic_len - 2) // 2 << 3) | (size_len - 1)
    first = bytes([flags]) + nonce + length.to_bytes(size_len, "big")
    blocks = [first]
    if aad:
        # Lengths below 2^16-2^8 take two bytes, which is every 802.11 header.
        if len(aad) >= 0xFF00:
            raise ValueError("additional data too long for this encoding")
        staged = len(aad).to_bytes(2, "big") + aad
        staged += b"\0" * (-len(staged) % BLOCK)
        blocks += [staged[i:i + BLOCK] for i in range(0, len(staged), BLOCK)]
    return blocks


def _ccm_mic(schedule, nonce: bytes, aad: bytes, payload: bytes,
             mic_len: int, size_len: int) -> bytes:
    """The CBC integrity code over the flags, the additional data and the
    payload, truncated to the length the caller asked for."""
    state = bytes(BLOCK)
    padded = payload + b"\0" * (-len(payload) % BLOCK)
    for block in (_ccm_blocks(nonce, aad, len(payload), mic_len, size_len)
                  + [padded[i:i + BLOCK] for i in range(0, len(padded), BLOCK)]):
        state = encrypt_block(schedule, bytes(x ^ y for x, y in zip(state, block)))
    return state[:mic_len]


def _ccm_counter(nonce: bytes, index: int, size_len: int) -> bytes:
    """Counter block `index`. Index zero encrypts the integrity code; the
    payload starts at one."""
    flags = size_len - 1
    return bytes([flags]) + nonce + index.to_bytes(size_len, "big")


def _ccm_stream(schedule, nonce: bytes, data: bytes, size_len: int) -> bytes:
    """Counter mode over the payload, starting at counter one."""
    out = bytearray()
    for i in range(0, len(data), BLOCK):
        block = encrypt_block(schedule,
                              _ccm_counter(nonce, i // BLOCK + 1, size_len))
        out += bytes(x ^ y for x, y in zip(data[i:i + BLOCK], block))
    return bytes(out)


def ccm_encrypt(key: bytes, nonce: bytes, aad: bytes, plain: bytes,
                mic_len: int = 8) -> bytes:
    """RFC 3610: the ciphertext with its integrity code appended.

    The length field takes what the nonce leaves of a block: fifteen bytes are
    shared between the nonce and the counter, so a thirteen-byte nonce -- which
    is the one CCMP uses -- leaves two.
    """
    size_len = 15 - len(nonce)
    if not 2 <= size_len <= 8:
        raise ValueError(f"a {len(nonce)}-byte nonce leaves no room for a length")
    if mic_len not in (4, 6, 8, 10, 12, 14, 16):
        raise ValueError(f"an integrity code is an even 4..16 bytes, not {mic_len}")
    schedule = expand_key(key)
    mic = _ccm_mic(schedule, nonce, aad, plain, mic_len, size_len)
    masked = bytes(x ^ y for x, y in
                   zip(mic, encrypt_block(schedule,
                                          _ccm_counter(nonce, 0, size_len))))
    return _ccm_stream(schedule, nonce, plain, size_len) + masked


def ccm_decrypt(key: bytes, nonce: bytes, aad: bytes, data: bytes,
                mic_len: int = 8) -> bytes | None:
    """The plaintext, or None if the integrity code does not check out.

    None rather than an exception: a frame that fails is a frame to drop, which
    is a thing that happens on a real link and not an error in this program.
    """
    if len(data) < mic_len:
        return None
    size_len = 15 - len(nonce)
    schedule = expand_key(key)
    body, carried = data[:-mic_len], data[-mic_len:]
    plain = _ccm_stream(schedule, nonce, body, size_len)
    mic = _ccm_mic(schedule, nonce, aad, plain, mic_len, size_len)
    masked = bytes(x ^ y for x, y in
                   zip(mic, encrypt_block(schedule,
                                          _ccm_counter(nonce, 0, size_len))))
    # Constant time is beside the point against an emulated station, but the
    # comparison is written as one anyway: it costs nothing and the habit is
    # worth more than the cycles.
    if sum(x ^ y for x, y in zip(masked, carried)) != 0:
        return None
    return plain
