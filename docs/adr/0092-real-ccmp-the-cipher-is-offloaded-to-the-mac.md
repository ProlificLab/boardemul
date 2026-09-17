# ADR 0092 — Real CCMP: the cipher is offloaded to the MAC, not the accelerator

**Status:** Accepted — 2026-09-17. The fourth workstream of ADR 0088, and it opens
by correcting the premise ADR 0084 left behind. This ADR is the discovery and the
plan; the peer's half and the MAC's half follow in their own ADRs.

## Context

ADR 0084 made the data cipher an identity: the station sends and accepts plaintext
behind a CCMP header, and the peer does the same. It recorded the assumption as
resting on the emulator's cipher being identity, and named the way out (lines
76-80): "If a future QEMU change makes the AES accelerator encrypt for real, the
station's own frames would become ciphertext and this would have to decrypt them."

That premise is wrong, and checking it is what this ADR is for.

**The AES accelerator already encrypts for real.** The fork models it in full
(`hw/misc/esp_aes.c`, a subclass instantiated for the S3 in `esp32s3.c`): both the
DMA path and the single-block "typical" path compute real AES through libgcrypt /
`AES_encrypt`, the TRIGGER register is wired to run them, and the board does not
shadow the 0x6003A000 window. The handshake proves it runs -- the GTK is AES-key-
wrapped in message three and the station unwraps it and completes. So the
accelerator is not the identity, and making it "encrypt for real" is not the task,
because it already does.

**The data cipher is offloaded to the WiFi MAC, which we do not model.**
Disassembled, the ROM's `ieee80211_crypto_encap` (0x4003adb4) is a seventy-byte
dispatcher: it selects by cipher type, sets a header length field, and returns.
There is no AES loop in it. It stages the CCMP header and hands the frame to the
hardware -- the WiFi MAC's own crypto engine encrypts the body on transmit and
decrypts it on receive, the way ESP32 WiFi always does pairwise data. Our
`esp_wifi_mac.c` moves the frame but does no crypto, so the body crosses the air
in the clear. That is the real reason for the plaintext ADR 0084 saw, and it is
consistent with ADR 0077: the pairwise key never appears in MMIO because it is
programmed into the MAC's crypto by the WiFi library through a path the register
trace does not capture.

## Decision

**Model the MAC's crypto, because that is what the real hardware does, and give it
the key from the peer, because the install path is opaque.** Real CCMP on the air
means the MAC encrypts on transmit and decrypts on receive; that belongs in the C
MAC device (`esp_wifi_mac.c`), which is the last thing a frame passes through
before the air and the first on the way back -- exactly where the hardware engine
sits. The firmware keeps handing it plaintext and keeps receiving plaintext, as it
does against real hardware; what changes is that the body on the air becomes real
AES-CCM ciphertext with a real MIC.

The one thing the model cannot take from the firmware is the key: ADR 0077 showed
the pairwise key never reaches MMIO, so there is nothing to intercept. But **the
peer derives the same key** -- the PTK, and its temporal key, come out of the
four-way handshake the peer runs, and are proven equal to the station's (ADR 0065,
the `wpa2` tests). So the peer hands the temporal key to the MAC device over the
frame channel when the handshake completes, and the device encrypts and decrypts
with it. The key is the real one; only its delivery is ours, which it has to be.

The work, each its own ADR and step, verified end to end because only a run shows
the firmware accepting a frame it decrypted:

1. **The peer's CCMP** (Python, `emu/aes.py` already has CCM): build the nonce and
   the additional-authenticated-data from an 802.11 frame per 802.11-2016, encrypt
   and MIC on transmit, decrypt and verify on receive. Fast-testable in isolation
   before any of it is on the air.
2. **The key handed over** the frame channel: a control message the peer sends the
   MAC device on handshake completion, carrying the temporal key.
3. **The MAC's CCMP** (C, `esp_wifi_mac.c`, gcrypt has CCM): encrypt the body of a
   Protected data frame on transmit, decrypt and verify on receive, with the key
   from step 2. Verified by a run: the firmware sends a frame the peer decrypts,
   and accepts the encrypted reply the peer sends.

## Consequences

**Good**

- It is faithful in the right place: the air carries genuine CCMP-encrypted 802.11
  frames with verified MICs, and the crypto sits where the hardware's does -- in
  the MAC -- so a capture of the air matches a real device, not an identity stand-
  in.
- It does not require cracking the WiFi library's opaque key install, which ADR
  0077 showed is not visible; the peer's derived key is the same key, and using it
  is honest because both ends of the air are ours (ADR 0064).
- No new dependency: Python CCM is in `emu/aes.py`, C CCM is in the gcrypt the AES
  device already links.

**Costs, accepted**

- **The key reaches the MAC model out of band.** Real hardware gets it from the
  key install; the model gets it from the peer over the frame channel. This is a
  modelling seam, recorded here so it is visible -- the alternative is reverse-
  engineering a key path ADR 0077 could not find in the trace, for no gain in
  fidelity on the air.
- **It is a C-tier change with a rebuild and a run per step**, the slowest loop in
  the project, and the change most able to break a working connection -- which is
  why ADR 0088 put it last.

## Alternatives rejected

**Model the AES accelerator harder.** It is already complete; the accelerator was
never the identity. Time there buys nothing.

**Intercept the firmware's key install and drive real MAC hardware crypto from
it.** The install does not touch MMIO (ADR 0077), so there is nothing to intercept
without reverse-engineering the closed WiFi library's key path -- effort with no
fidelity gain over using the peer's identical derived key.

**Do all the crypto in the peer and leave the MAC a pass-through.** The firmware's
receive path expects the MAC to have decrypted; hand its stack ciphertext and it
rejects the frame. The decrypt has to live in the MAC, where the hardware's does.
