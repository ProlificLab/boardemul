#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The WPA2-PSK key schedule, and the EAPOL-Key frame that carries it.

Everything here is hashing and bookkeeping. The hashing is `hashlib`'s --
SHA1, HMAC and PBKDF2 are all in the standard library, which is half of why
the peer moved to Python (ADR 0064). The cipher it does not have is next door
in `aes.py`.

The names are the standard's: a pairwise master key derived from the
passphrase and the network name, a pairwise transient key derived from that
and the two nonces, and three keys carved out of it -- one to sign the
handshake with, one to wrap the group key with, and one to encrypt data with.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass, field

from emu import aes

PMK_BYTES = 32
PTK_BYTES = 48                  # KCK 16, KEK 16, TK 16, for CCMP
NONCE_BYTES = 32
MIC_BYTES = 16
PBKDF2_ROUNDS = 4096            # the standard's, and the reason this is slow

# EAPOL-Key, as the third and later editions of 802.11 lay it out.
EAPOL_VERSION = 2
EAPOL_TYPE_KEY = 3
KEY_TYPE_RSN = 2                # descriptor type: RSN rather than WPA1
# Key Information bits.
KI_VERSION_AES = 2              # HMAC-SHA1 integrity code, AES key wrap
KI_PAIRWISE = 1 << 3
KI_INSTALL = 1 << 6
KI_ACK = 1 << 7
KI_MIC = 1 << 8
KI_SECURE = 1 << 9
KI_ENCRYPTED = 1 << 12


def pmk(passphrase: str, ssid: str) -> bytes:
    """The pairwise master key: the passphrase stretched over the network name.

    Four thousand and ninety-six rounds, which is the standard's number and
    takes a few milliseconds here. It depends on nothing that changes during a
    run, so a peer works it out once.
    """
    return hashlib.pbkdf2_hmac("sha1", passphrase.encode(), ssid.encode(),
                               PBKDF2_ROUNDS, PMK_BYTES)


def prf(key: bytes, label: bytes, data: bytes, length: int) -> bytes:
    """The HMAC-SHA1 pseudo-random function the key schedule is built from.

    Successive HMACs over label, zero, data, counter -- the counter is what
    makes each twenty-byte slice different, and the result is truncated to the
    length asked for rather than to a whole number of slices.
    """
    out = b""
    counter = 0
    while len(out) < length:
        out += hmac.new(key, label + b"\0" + data + bytes([counter]),
                        hashlib.sha1).digest()
        counter += 1
    return out[:length]


def ptk(master: bytes, ap: bytes, station: bytes,
        anonce: bytes, snonce: bytes) -> bytes:
    """The pairwise transient key.

    The addresses go in smaller first and so do the nonces, independently,
    which is how both ends reach the same key without agreeing who is who.
    That ordering is also the one property of this function worth asserting:
    swap either pair and nothing changes.
    """
    data = (min(ap, station) + max(ap, station)
            + min(anonce, snonce) + max(anonce, snonce))
    return prf(master, b"Pairwise key expansion", data, PTK_BYTES)


@dataclass
class Keys:
    """A pairwise transient key, split the way the standard splits it."""
    ptk: bytes

    @property
    def kck(self) -> bytes:
        """Signs the handshake's integrity codes."""
        return self.ptk[0:16]

    @property
    def kek(self) -> bytes:
        """Wraps the group key in the third message."""
        return self.ptk[16:32]

    @property
    def tk(self) -> bytes:
        """Encrypts data frames."""
        return self.ptk[32:48]


def eapol_key(key_info: int, nonce: bytes = b"", replay: int = 1,
              key_data: bytes = b"", key_len: int = 16,
              key_rsc: bytes = b"\0" * 8, key_mic: bytes = b"") -> bytes:
    """One EAPOL-Key frame, without the LLC header in front of it.

    The four-byte EAPOL header, then the ninety-five byte key descriptor, then
    the key data. The integrity code goes in as zeroes when it is being
    computed and is patched in afterwards, which is what the standard means by
    signing the frame with the field set to zero.
    """
    body = (bytes([KEY_TYPE_RSN])
            + key_info.to_bytes(2, "big")
            + key_len.to_bytes(2, "big")
            + replay.to_bytes(8, "big")
            + (nonce or bytes(NONCE_BYTES))
            + bytes(16)                     # key IV, unused with AES key wrap
            + key_rsc
            + bytes(8)                      # key identifier, reserved
            + (key_mic or bytes(MIC_BYTES))
            + len(key_data).to_bytes(2, "big")
            + key_data)
    return (bytes([EAPOL_VERSION, EAPOL_TYPE_KEY])
            + len(body).to_bytes(2, "big") + body)


# Where the integrity code sits inside the frame above: past the four-byte
# EAPOL header and the descriptor fields before it.
MIC_OFFSET = 4 + 1 + 2 + 2 + 8 + NONCE_BYTES + 16 + 8 + 8


def sign(frame: bytes, kck: bytes) -> bytes:
    """The same frame with its integrity code filled in.

    Taken over the whole frame with the field zeroed, which it already is when
    `eapol_key` built it, and truncated to sixteen bytes -- key descriptor
    version two, which is what an RSN network using CCMP asks for.
    """
    mic = hmac.new(kck, frame, hashlib.sha1).digest()[:MIC_BYTES]
    return frame[:MIC_OFFSET] + mic + frame[MIC_OFFSET + MIC_BYTES:]


def check(frame: bytes, kck: bytes) -> bool:
    """Whether a frame the station sent carries the code it should.

    The code is verified against the frame with the field zeroed again, which
    is the only way round it can work: the sender could not have included it
    in its own input.
    """
    if len(frame) < MIC_OFFSET + MIC_BYTES:
        return False
    carried = frame[MIC_OFFSET:MIC_OFFSET + MIC_BYTES]
    zeroed = frame[:MIC_OFFSET] + bytes(MIC_BYTES) + frame[MIC_OFFSET + MIC_BYTES:]
    want = hmac.new(kck, zeroed, hashlib.sha1).digest()[:MIC_BYTES]
    return hmac.compare_digest(carried, want)


def key_info(frame: bytes) -> int:
    """The Key Information field, which says which message this is."""
    return int.from_bytes(frame[5:7], "big")


def nonce_of(frame: bytes) -> bytes:
    """The sender's nonce, out of the descriptor."""
    at = 4 + 1 + 2 + 2 + 8
    return frame[at:at + NONCE_BYTES]


def gtk_kde(gtk: bytes, key_id: int = 1) -> bytes:
    """The group key, in the key data element the third message carries.

    Type 00-0F-AC:1 is the group key; the first byte holds the key identifier
    and the second is reserved, then the key itself.
    """
    body = b"\x00\x0f\xac\x01" + bytes([key_id & 3, 0]) + gtk
    return bytes([0xDD, len(body)]) + body


def wrap_key_data(kek: bytes, data: bytes) -> bytes:
    """Key data, padded and wrapped for the third message.

    AES key wrap works on eight-byte blocks and needs at least two, so short
    data is padded -- with 0xdd and then zeroes, which is what the standard
    says to use and what lets the station tell padding from an element.
    """
    if len(data) % 8:
        data += b"\xdd" + bytes(-(len(data) + 1) % 8)
    return aes.key_wrap(kek, data)


@dataclass
class Handshake:
    """One station's four-way handshake, as a small state machine.

    It holds the nonce it made, the keys once they can be derived, and how far
    the exchange has got. The peer drives it: hand it what the station sent and
    it says what to send back, or None when there is nothing to say.
    """
    master: bytes
    ap: bytes
    station: bytes
    anonce: bytes = field(default_factory=lambda: os.urandom(NONCE_BYTES))
    snonce: bytes = b""
    keys: Keys | None = None
    replay: int = 1
    done: bool = False
    rsn_element: bytes = b""
    gtk: bytes = field(default_factory=lambda: os.urandom(16))
    # The message three actually sent, cached so a retransmitted message two
    # gets the same frame back rather than a fresh one (ADR 0078).
    _third: bytes = b""

    def first(self) -> bytes:
        """Message one: the access point's nonce, unsigned.

        Unsigned because the station has no key yet -- it derives one from this
        nonce, and the second message is the first that can carry a code.
        """
        return eapol_key(KI_VERSION_AES | KI_PAIRWISE | KI_ACK,
                         nonce=self.anonce, replay=self.replay)

    def third(self) -> bytes:
        """Message three: install the key, and here is the group key.

        Signed, and with the key data wrapped -- which is the one place the
        handshake needs a block cipher.
        """
        assert self.keys is not None, "no keys yet: message two has not arrived"
        self.replay += 1
        data = self.rsn_element + gtk_kde(self.gtk)
        frame = eapol_key(
            KI_VERSION_AES | KI_PAIRWISE | KI_INSTALL | KI_ACK | KI_MIC
            | KI_SECURE | KI_ENCRYPTED,
            nonce=self.anonce, replay=self.replay,
            key_data=wrap_key_data(self.keys.kek, data))
        return sign(frame, self.keys.kck)

    def take(self, frame: bytes) -> bytes | None:
        """What the station sent; what to send back, if anything.

        Message two brings the station's nonce, which is everything still
        missing from the key schedule, so the keys are derived here and the
        code on that very frame is the first proof the passphrase matched.
        Message four is an acknowledgement and ends it.
        """
        info = key_info(frame)
        if not info & KI_MIC:
            return None                     # nothing unsigned is expected here
        if self.keys is None:
            self.snonce = nonce_of(frame)
            self.keys = Keys(ptk(self.master, self.ap, self.station,
                                 self.anonce, self.snonce))
            if not check(frame, self.keys.kck):
                # The passphrase did not match, or the nonces did not. Either
                # way there is nothing to answer and the station will retry.
                self.keys = None
                return None
            self._third = self.third()
            return self._third
        if not check(frame, self.keys.kck):
            return None
        # A signed frame once the keys are derived is message four only if it
        # carries the Secure bit (key_info 0x030a). Without it (0x010a) it is
        # the station retransmitting message two because it has not accepted
        # message three -- and taking that for message four is how the peer
        # reported a handshake that never happened (ADR 0078). Resend the same
        # message three, unchanged, and go on waiting for a real message four.
        if key_info(frame) & KI_SECURE:
            self.done = True
            return None
        return self._third
