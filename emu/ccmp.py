#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""CCMP: the 802.11 framing around AES-CCM, so a data frame is really encrypted.

The cipher itself is in `emu/aes.py` (CCM over an AES-128 pinned to FIPS-197).
What this adds is the part 802.11 wraps around it and the part that is easy to
get wrong: the nonce built from the transmitter address and the packet number,
and the additional-authenticated-data built from the MPDU header with the right
bits masked. Get either wrong and the integrity code does not match, exactly the
way a hand-counted offset in the handshake did.

It is written to 802.11-2016 §12.5.3. Both ends of the air use it -- the peer
here and the MAC model in QEMU -- so they agree by construction, and because it
follows the standard the bytes on the air are what a real capture would show. It
covers data frames, QoS and not, without the fourth address (this network is not
a WDS bridge); an A4 frame is refused rather than mishandled.
"""

from __future__ import annotations

from emu import aes

MIC_LEN = 8                     # CCMP-128 carries an eight-octet integrity code
KEY_LEN = 16                    # the temporal key is AES-128

# Frame Control masks for the AAD (§12.5.3.3.3). Retry, Power Management and More
# Data are cleared because they change in flight and must not be authenticated;
# Protected is forced on; for a data frame the subtype bits also clear.
_FC1_CLEAR = 0x38               # Retry | PwrMgmt | MoreData, in the second octet
_FC1_PROTECTED = 0x40           # the Protected bit, forced on
_FC0_SUBTYPE = 0x70             # subtype bits masked for a (non-management) data frame


def build_header(pn: int, key_id: int = 0) -> bytes:
    """The eight-octet CCMP header carrying a packet number.

    The packet number is 48 bits, split oddly: its two lowest octets first, then
    a reserved octet and the key-id octet (with the Ext IV bit set), then the top
    four octets. The header rides in the clear; only the body is encrypted.
    """
    return bytes([pn & 0xFF, (pn >> 8) & 0xFF, 0x00, 0x20 | (key_id << 6),
                  (pn >> 16) & 0xFF, (pn >> 24) & 0xFF,
                  (pn >> 32) & 0xFF, (pn >> 40) & 0xFF])


def header_pn(ccmp_header: bytes) -> int:
    """The packet number out of a CCMP header, reversing build_header's split."""
    b = ccmp_header
    return (b[0] | (b[1] << 8) | (b[4] << 16) | (b[5] << 24)
            | (b[6] << 32) | (b[7] << 40))


def _hdr_len(mac_header: bytes) -> int:
    """The management/data header length: twenty-four octets, twenty-six if it is
    a QoS data frame (a QoS Control field follows the third address)."""
    return 26 if _is_qos(mac_header) else 24


def _is_qos(mac_header: bytes) -> bool:
    return (mac_header[0] & 0x0C) == 0x08 and bool(mac_header[0] & 0x80)


def _has_a4(mac_header: bytes) -> bool:
    return (mac_header[1] & 0x03) == 0x03          # ToDS and FromDS both set


def nonce(mac_header: bytes, pn: int) -> bytes:
    """The thirteen-octet CCM nonce: a flags octet, the transmitter address, the
    packet number big-endian (§12.5.3.3.4).

    The flags octet carries the priority -- the QoS TID for a QoS frame, zero
    otherwise -- and a management bit. The transmitter address is address two.
    """
    mgmt = (mac_header[0] & 0x0C) == 0x00
    priority = (mac_header[24] & 0x0F) if _is_qos(mac_header) else 0
    flags = priority | (0x10 if mgmt else 0)
    a2 = mac_header[10:16]
    return bytes([flags]) + a2 + pn.to_bytes(6, "big")


def aad(mac_header: bytes) -> bytes:
    """The additional-authenticated-data from the MPDU header (§12.5.3.3.3).

    The Frame Control has the volatile bits masked and Protected forced on; the
    three addresses go in as they are; the Sequence Control keeps only its
    fragment number, the sequence number masked because it changes on a
    retransmission; a QoS frame appends its QoS Control with only the TID kept.
    """
    if _has_a4(mac_header):
        raise ValueError("A4 (WDS) frames are not on this network")
    fc0 = mac_header[0]
    if (mac_header[0] & 0x0C) != 0x00:              # not a management frame
        fc0 &= ~_FC0_SUBTYPE
    fc1 = (mac_header[1] & ~_FC1_CLEAR) | _FC1_PROTECTED
    out = bytes([fc0, fc1]) + mac_header[4:10] + mac_header[10:16] + mac_header[16:22]
    out += bytes([mac_header[22] & 0x0F, 0x00])    # Sequence Control, sequence masked
    if _is_qos(mac_header):
        out += bytes([mac_header[24] & 0x0F, 0x00])  # QoS Control, only the TID
    return out


def encrypt(tk: bytes, mac_header: bytes, ccmp_header: bytes,
            body: bytes) -> bytes:
    """Encrypt a data frame's body and append its MIC.

    `body` is the plaintext MSDU -- the LLC/SNAP header and what rides on it. The
    result is the ciphertext, the same length, followed by the eight-octet MIC.
    The MAC header and CCMP header are the ones that will be on the air, because
    the header's fields are authenticated and the packet number is the nonce.
    """
    pn = header_pn(ccmp_header)
    return aes.ccm_encrypt(tk, nonce(mac_header, pn), aad(mac_header), body,
                           mic_len=MIC_LEN)


def decrypt(tk: bytes, mac_header: bytes, ccmp_header: bytes,
            sealed: bytes) -> bytes | None:
    """Verify and decrypt a data frame's body, or None if the MIC does not match.

    `sealed` is the ciphertext followed by its MIC. None means the frame was not
    produced by this key over this header -- a wrong key, a changed header, or a
    forged frame -- and it must be dropped, not passed on.
    """
    pn = header_pn(ccmp_header)
    return aes.ccm_decrypt(tk, nonce(mac_header, pn), aad(mac_header), sealed,
                           mic_len=MIC_LEN)
