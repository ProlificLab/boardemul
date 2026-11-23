#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""tools/ble_central.py's link layer, without a board (ADR 0171).

What it puts on the air is checked against the Bluetooth Core
Specification's own sample data where there is some: the session key a
long-term key and two diversifiers give, and the first encrypted packet of
an encryption start (Vol 6, Part C, 1). The rest -- reading the advertiser's
address, splitting a frame into packets any link layer accepts, decrypting
what the peripheral sends -- is checked against what those rules say.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import ble_central as bc  # noqa: E402
from tools import ble_crypto  # noqa: E402

# The specification's encryption sample data, written most significant first
# as it is printed.
LTK = bytes.fromhex("4C68384139F574D836BCF34E9DFB01BF")
SKDM, SKDS = bytes.fromhex("ACBDCEDFE0F10213"), bytes.fromhex("0213243546576879")
IVM, IVS = bytes.fromhex("BADCAB24"), bytes.fromhex("DEAFBABE")
SK = bytes.fromhex("99AD1B5226A37E3E058E3B8E27C2C666")


class Air:
    """Stands in for the air: what was sent, and what will be heard."""

    def __init__(self, heard=()):
        self.sent: list[bytes] = []
        self.heard = list(heard)

    def send(self, frame: bytes) -> None:
        self.sent.append(frame)

    def poll(self) -> list[bytes]:
        out, self.heard = self.heard, []
        return out

    def close(self) -> None:
        pass


def central(tmp_path: Path, heard=()) -> bc.Central:
    c = bc.Central(tmp_path)
    c.air.close()
    c.air = Air(heard)
    return c


def test_the_session_key_matches_the_specification(tmp_path) -> None:
    c = central(tmp_path)
    c.starting = (LTK[::-1], SKDM[::-1], IVM[::-1])
    # The peripheral's LL_ENC_RSP: SKDs and IVs, least significant first.
    c.air.heard = [bc.on(bc.CONN_AA, bytes([0x03, bc.LL_ENC_RSP]) + SKDS[::-1] + IVS[::-1])]
    c._listen()
    assert c.sk == SK
    assert c.iv == IVM[::-1] + IVS[::-1]


def test_the_encryption_start_answer_matches_the_specification(tmp_path) -> None:
    """LL_START_ENC_RSP from the central, packet counter 0: on the air it is
    0x0F, then 9F CD A7 F4 48 -- its one octet encrypted, and the MIC."""
    c = central(tmp_path)
    c.sk, c.iv, c.tx_crypt = SK, IVM[::-1] + IVS[::-1], True
    c.nesn = c.sn = 1
    c._send(bc.LLID_CONTROL, bytes([bc.LL_START_ENC_RSP]))
    assert c.air.sent[-1] == bc.on(bc.CONN_AA, bytes.fromhex("0f9fcda7f448"))
    assert c.tx_count == 1


def test_an_empty_packet_is_never_encrypted(tmp_path) -> None:
    c = central(tmp_path)
    c.sk, c.iv, c.tx_crypt = SK, bytes(8), True
    c._send(bc.LLID_CONTINUE)
    assert c.air.sent[-1] == bc.on(bc.CONN_AA, bytes([0x01]))
    assert c.tx_count == 0


def test_what_the_peripheral_encrypts_is_read(tmp_path) -> None:
    c = central(tmp_path)
    c.sk, c.iv, c.rx_crypt = SK, IVM[::-1] + IVS[::-1], True
    header = 0x03                                  # control, NESN 0, SN 0
    nonce = (0).to_bytes(5, "little") + c.iv       # direction 0: the peripheral's
    sealed = ble_crypto.ccm_encrypt(SK, nonce, bytes([bc.LL_START_ENC_RSP]),
                                    aad=bytes([header & 0xE3]))
    c.air.heard = [bc.on(bc.CONN_AA, bytes([header]) + sealed)]
    c._listen()
    assert c.control[-1] == bytes([bc.LL_START_ENC_RSP])
    assert c.rx_count == 1 and c.mic_failures == 0


def test_a_packet_that_does_not_check_is_not_read(tmp_path) -> None:
    c = central(tmp_path)
    c.sk, c.iv, c.rx_crypt = SK, bytes(8), True
    c.air.heard = [bc.on(bc.CONN_AA, bytes([0x03, 0x06, 1, 2, 3, 4]))]
    c._listen()
    assert not c.control and c.mic_failures == 1


def test_the_advertisers_address_is_read_and_answered(tmp_path) -> None:
    address = bytes.fromhex("02004d445402")
    advert = bytes([bc.TX_ADD]) + address + bytes.fromhex("020106")
    c = central(tmp_path, heard=[bc.on(bc.ADV_AA, advert)])
    assert c.connect(wait_s=1)
    assert c.peer == address and c.peer_random
    aa, request = bc.split(c.air.sent[-1])
    assert aa == bc.ADV_AA, "a connection request goes on the advertising address"
    assert request[0] == bc.PDU_CONNECT_IND | bc.RX_ADD
    assert request[1:7] == bc.INITIATOR and request[7:13] == address


def test_a_frame_goes_in_packets_any_link_layer_takes(tmp_path) -> None:
    c = central(tmp_path)
    c.send_l2cap(bc.CID_SMP, bytes([bc.SMP_PUBLIC_KEY]) + bytes(64))
    sizes = [len(p) for _, p in c.outbox]
    assert sizes == [27, 27, 15]
    assert [llid for llid, _ in c.outbox] == [bc.LLID_START, bc.LLID_CONTINUE,
                                              bc.LLID_CONTINUE]
    assert c.outbox[0][1][:4] == bytes([65, 0, bc.CID_SMP, 0])


def test_a_notification_is_kept_apart_from_answers(tmp_path) -> None:
    c = central(tmp_path)
    att = bytes([bc.ATT_NOTIFY]) + (0x0010).to_bytes(2, "little") + bytes([7, 0, 0, 0])
    c.air.heard = [bc.on(bc.CONN_AA, bytes([bc.LLID_START]) + len(att).to_bytes(2, "little")
                         + bc.CID_ATT.to_bytes(2, "little") + att)]
    c._listen()
    assert c.notifications == [(0x0010, bytes([7, 0, 0, 0]))]
    assert not c.frames, "a request would have taken it for its answer"


def test_an_indication_is_confirmed(tmp_path) -> None:
    c = central(tmp_path)
    att = bytes([bc.ATT_INDICATE]) + (0x0012).to_bytes(2, "little") + b"x"
    c.air.heard = [bc.on(bc.CONN_AA, bytes([bc.LLID_START]) + len(att).to_bytes(2, "little")
                         + bc.CID_ATT.to_bytes(2, "little") + att)]
    c._listen()
    assert c.notifications == [(0x0012, b"x")]
    assert c.outbox[-1] == (bc.LLID_START, bytes([1, 0, bc.CID_ATT, 0, bc.ATT_CONFIRM]))


def test_only_its_own_connection_and_advertising_are_heard(tmp_path) -> None:
    """ADR 0173: another connection's frames are not this central's, and a
    second board's advertising does not mean this link was lost."""
    c = central(tmp_path)
    c.peer, c.connected_at = bytes.fromhex("02004d445402"), 0.0
    other_board = bytes.fromhex("03004d445402")
    c.air.heard = [bc.on(0x12345678, bytes([0x03, bc.LL_START_ENC_RSP])),
                   bc.on(bc.ADV_AA, bytes([0]) + other_board + bytes.fromhex("020106"))]
    c._listen()
    assert not c.control and c.nesn == 0
    assert c.adverts_since == 0
    c.air.heard = [bc.on(bc.ADV_AA, bytes([0]) + c.peer + bytes.fromhex("020106"))]
    c._listen()
    assert c.adverts_since == 1, "its own peripheral advertising again is"
