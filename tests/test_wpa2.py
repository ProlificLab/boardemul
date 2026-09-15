#!/usr/bin/env python3
"""The WPA2 key schedule, and a handshake run against itself.

The master key has published vectors and gets them. The rest has none that can
be quoted honestly here, so what is checked is the properties that make the
scheme work at all -- above all that both ends reach the same key without
agreeing which of them is which, which is the whole point of the min/max
ordering and the easiest thing to get backwards.

The handshake is then driven from the station's side by this file: a second
Handshake, given the other end's nonce, produces the frames a real station
would and the first one has to accept them. That is not proof against the
firmware, but it does say the two directions agree about the integrity code,
the wrapping and the field offsets -- which is where the bugs are.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import aes, wpa2  # noqa: E402

AP = bytes.fromhex("020000000001")
STATION = bytes.fromhex("025444_4d0001".replace("_", ""))


def test_the_master_key_matches_the_published_vectors() -> None:
    """Two passphrase-and-name pairs quoted with the standard's mapping. The
    hashing is `hashlib`'s, so what this really pins is the argument order,
    the round count and the length -- every one of which is easy to get wrong
    and silent when it is."""
    for passphrase, ssid, want in [
        ("password", "IEEE",
         "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e"),
        ("ThisIsAPassword", "ThisIsASSID",
         "0dc0d6eb90555ed6419756b9a15ec3e3209b63df707dd508d14581f8982721af"),
    ]:
        assert wpa2.pmk(passphrase, ssid).hex() == want, f"{passphrase}/{ssid}"


def test_the_transient_key_does_not_care_who_is_who() -> None:
    """The addresses go in smaller first and so do the nonces, independently.
    Swap either pair and the key must not move -- that is what lets the station
    and the access point derive the same key, and reversing one of the two
    orderings is the classic way to get a handshake that never verifies."""
    master = wpa2.pmk("AA15994823428", "xinyuandianzi")
    a, b = bytes(range(32)), bytes(range(32, 64))
    straight = wpa2.ptk(master, AP, STATION, a, b)
    assert wpa2.ptk(master, STATION, AP, a, b) == straight, "the addresses"
    assert wpa2.ptk(master, AP, STATION, b, a) == straight, "the nonces"
    assert wpa2.ptk(master, STATION, AP, b, a) == straight, "both"


def test_the_transient_key_depends_on_everything_else() -> None:
    """Symmetry in the pairs, and nothing else. A key that ignored a nonce
    would be the same for every session, which is worse than wrong."""
    master = wpa2.pmk("AA15994823428", "xinyuandianzi")
    a, b = bytes(range(32)), bytes(range(32, 64))
    base = wpa2.ptk(master, AP, STATION, a, b)
    assert len(base) == wpa2.PTK_BYTES
    assert wpa2.ptk(master, AP, bytes(6), a, b) != base, "the station address"
    assert wpa2.ptk(master, AP, STATION, bytes(32), b) != base, "a nonce"
    assert wpa2.ptk(bytes(32), AP, STATION, a, b) != base, "the master key"


def test_the_keys_are_carved_up_in_order() -> None:
    keys = wpa2.Keys(bytes(range(48)))
    assert keys.kck == bytes(range(0, 16))
    assert keys.kek == bytes(range(16, 32))
    assert keys.tk == bytes(range(32, 48))


def test_signing_fills_the_field_the_check_reads() -> None:
    """The code is taken over the frame with its own field zeroed, which is the
    only way both ends can compute the same thing. If `sign` and `check`
    disagreed about where the field is, or about zeroing it, this fails."""
    kck = bytes(range(16))
    frame = wpa2.eapol_key(wpa2.KI_MIC | wpa2.KI_PAIRWISE, nonce=bytes(range(32)))
    assert not wpa2.check(frame, kck), "an unsigned frame must not verify"
    signed = wpa2.sign(frame, kck)
    assert wpa2.check(signed, kck)
    assert len(signed) == len(frame), "signing must not change the length"
    assert not wpa2.check(signed, bytes(16)), "the wrong key must not verify"
    broken = bytearray(signed)
    broken[wpa2.MIC_OFFSET - 1] ^= 1
    assert not wpa2.check(bytes(broken), kck), "a changed frame must not verify"


def test_the_nonce_comes_back_out_where_it_went_in() -> None:
    """The offsets in this file are all hand-counted, so this reads one back."""
    nonce = bytes(range(100, 132))
    assert wpa2.nonce_of(wpa2.eapol_key(0, nonce=nonce)) == nonce


def test_the_group_key_element_is_wrapped_and_unwrappable_in_shape() -> None:
    """Key data is padded to whole eight-byte blocks and grows by the wrap's
    check value. The station has to be able to find the element inside it, so
    the length has to come out where it expects."""
    kek = bytes(range(16))
    data = wpa2.gtk_kde(bytes(range(16)))
    assert data[0] == 0xDD and data[1] == len(data) - 2
    wrapped = wpa2.wrap_key_data(kek, data)
    assert len(wrapped) % 8 == 0
    assert len(wrapped) >= len(data) + 8


def _station_side(master: bytes, ap: bytes, station: bytes,
                  first: bytes) -> tuple[bytes, wpa2.Keys]:
    """What a station does with message one: pick a nonce, derive the keys, and
    sign message two. Written here rather than imported so that a bug in the
    peer cannot hide by being on both sides of the comparison."""
    anonce = wpa2.nonce_of(first)
    snonce = bytes(range(200, 232))
    keys = wpa2.Keys(wpa2.ptk(master, ap, station, anonce, snonce))
    second = wpa2.eapol_key(wpa2.KI_VERSION_AES | wpa2.KI_PAIRWISE | wpa2.KI_MIC,
                            nonce=snonce, replay=1)
    return wpa2.sign(second, keys.kck), keys


def test_a_handshake_completes_against_a_station_that_plays_along() -> None:
    """All four messages, with the station's half written independently above.

    The access point signs two and three with a key it derived from the
    station's nonce; the station verifies three with a key it derived from the
    access point's. They only agree if the ordering, the offsets, the zeroing
    and the truncation all match.
    """
    master = wpa2.pmk("AA15994823428", "xinyuandianzi")
    shake = wpa2.Handshake(master=master, ap=AP, station=STATION,
                           rsn_element=bytes.fromhex(
                               "30140100000fac040100000fac040100000fac020000"))

    first = shake.first()
    assert wpa2.key_info(first) & wpa2.KI_MIC == 0, "message one is unsigned"

    second, station_keys = _station_side(master, AP, STATION, first)
    third = shake.take(second)
    assert third is not None, "message two was not accepted"
    assert shake.keys is not None and shake.keys.ptk == station_keys.ptk, \
        "the two ends derived different keys"
    assert wpa2.check(third, station_keys.kck), "the station cannot verify three"
    info = wpa2.key_info(third)
    for bit, name in ((wpa2.KI_INSTALL, "install"), (wpa2.KI_MIC, "mic"),
                      (wpa2.KI_SECURE, "secure"), (wpa2.KI_ENCRYPTED, "encrypted")):
        assert info & bit, f"message three is missing the {name} bit"

    fourth = wpa2.sign(
        wpa2.eapol_key(wpa2.KI_VERSION_AES | wpa2.KI_PAIRWISE | wpa2.KI_MIC
                       | wpa2.KI_SECURE, replay=shake.replay),
        station_keys.kck)
    assert shake.take(fourth) is None, "there is nothing to say after four"
    assert shake.done, "the handshake did not finish"


def test_a_wrong_passphrase_gets_nowhere() -> None:
    """The integrity code on message two is the first thing that proves the
    passphrase, and a station using a different one must not get message
    three -- which would hand it a wrapped group key it could not read."""
    right = wpa2.pmk("AA15994823428", "xinyuandianzi")
    wrong = wpa2.pmk("not-the-passphrase", "xinyuandianzi")
    shake = wpa2.Handshake(master=right, ap=AP, station=STATION)
    second, _ = _station_side(wrong, AP, STATION, shake.first())
    assert shake.take(second) is None, "a bad code was accepted"
    assert shake.keys is None and not shake.done


def test_the_key_it_ends_with_encrypts() -> None:
    """The point of all of it: a temporal key that CCM will take."""
    master = wpa2.pmk("AA15994823428", "xinyuandianzi")
    shake = wpa2.Handshake(master=master, ap=AP, station=STATION)
    second, _ = _station_side(master, AP, STATION, shake.first())
    shake.take(second)
    assert shake.keys is not None
    sealed = aes.ccm_encrypt(shake.keys.tk, bytes(range(13)), b"aad", b"payload")
    assert aes.ccm_decrypt(shake.keys.tk, bytes(range(13)), b"aad", sealed) == b"payload"
