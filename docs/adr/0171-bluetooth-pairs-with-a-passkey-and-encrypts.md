# ADR 0171 — Bluetooth pairs with a passkey and encrypts the link

**Status:** Accepted — 2026-11-20. Takes step 4 of ADR 0157's road, which
ADR 0169 left: pairing and the link's encryption. Amends ADR 0046's
advertising frame.

## Context

Meshtastic's Bluetooth pairs with a PIN unless told otherwise: RANDOM_PIN on
a node with a screen (the T-Deck, which shows it), FIXED_PIN otherwise. A
phone pairs by LE Secure Connections passkey entry, the link is encrypted,
and only then do ToRadio and FromRadio answer. ADR 0169 read the node only
after switching it to NO_PIN. Three things were missing for the default:

1. **The controller's AES engine.** The link's session key is
   `SK = e(LTK, SKD)`, and the controller computes it on the Bluetooth core's
   own AES unit, which nothing modelled: a start was never answered.
2. **AES-CCM on every packet.** Once the link is encrypted the core
   encrypts what it transmits and decrypts what it receives, with a 4-octet
   MIC (Core Specification Vol 6, Part E).
3. **A central that pairs.** `tools/ble_central.py` spoke ATT only.

And a fourth surfaced on the way: pairing hashes both devices' addresses into
its keys (f5, f6), and a central learns the peripheral's address from its
advertisements. Our advertising frame (ADR 0046) carried the type and the
advertising data, not the advertiser's address.

### What the ROM says

All read off the S3's ROM (`esp32s3_rev0_rom.bin`, resolved through its stubs
with `tools/rom_symbols.py` and disassembled with the toolchain's objdump):

- **`r_rwip_aes_encrypt`** (0x4002c204) copies the 16-octet block into
  exchange memory at 0x128, writes the key as four little-endian words to
  +0xB4, +0xB8, +0xBC, +0xC0 (key octet 0 lowest), writes 0x128 to +0xC4,
  acknowledges status bit 7 (+0x18), enables it (+0x0C) and sets bit 0 of
  +0xB0. Its interrupt handler (`r_rwip_crypt_isr_handler`, 0x4002ba7c)
  masks bit 7 and signals a kernel event; the event handler (0x4002ba44)
  copies the result from exchange memory 0x138, the block's pointer plus 16.
  The order is least significant octet first in registers and memory alike,
  HCI_LE_Encrypt's order, so the unit reverses into FIPS-197's order and
  back. The link's MIC agreeing with a central that follows the
  specification is the check that this is right.
- **`r_lld_con_enc_key_load`** (0x4001cb5c) writes the session key into the
  link's control structure at +44..+59 reversed, most significant octet
  first; the IV at +60..+67 as given (IVm, then IVs); and zeroes +68..+79,
  the two 39-bit packet counters.
- **`r_lld_con_tx_enc`** (0x4001ca80) and **`r_lld_con_rx_enc`** (0x4001caf0)
  set and clear bits 9 and 8 of the control structure's +2: encrypt what is
  sent, decrypt what is received.
- **`r_lld_con_rx`** (0x4001a905) takes 4 off a received length of 5 or more
  when the link decrypts: the core reports the length on the air, MIC
  included, and the controller removes it.
- **`r_lld_con_tx_prog_new_packet`** (0x4001aea3) adds 4 to the length it
  stages in a transmit descriptor when the link encrypts, and the
  transmit-done handler (0x4001a9f9) takes 4 off a length of 5 or more: on
  the way out too, the descriptor holds the length on the air. The first
  model encrypted all of it and appended a MIC, and a central read four
  octets of the next fragment twice — `meshtastic.pool.ntp.ntp.o` — which
  is how this was found.
- The advertising descriptor's header (transmit entry 0, ADR 0168) is 0x20
  on Meshtastic: ADV_IND, TxAdd 0, a public address. The address itself is
  control structure 0's +6..+11, least significant octet first.

## Decision

1. **The core's AES unit** (`qemu-patch/esp_ble_core.c`): a write of bit 0
   to +0xB0 reads the block at the pointer in +0xC4, encrypts it under the
   key in +0xB4..+0xC0, writes the result 16 octets on, clears the start
   bit, and raises status bit 7 through the interrupt FIFO like every other
   bit. Synchronous, as QEMU's other cipher units are; a block takes the
   silicon microseconds.
2. **AES-CCM on the link**, from the control structure the controller
   loaded:
   - transmit, with +2 bit 9 set and a staged length of 5 or more: encrypt
     the first length − 4 octets under the key at +44 and put the MIC in
     the last four, the nonce being the transmit counter and direction bit
     then the IV at +60; the additional data is the header's first octet
     with NESN, SN and MD cleared. The counter counts packets encrypted.
   - receive, with +2 bit 8 set and a non-empty payload: decrypt; the
     descriptor gets the plaintext and the length on the air, MIC
     included, which the controller takes 4 from. A packet whose MIC does
     not check is dropped as one lost on the air would be, and logged; the
     controller is not told.
   - The counters live in the control structure (+68 transmit, +74
     receive, six octets each) where the controller zeroes them; which of
     the two halves is which is our assignment, since nothing read so far
     reads them back.
   - The board is the peripheral (ADR 0157), so what it sends carries
     direction 0 and what it hears direction 1.
3. **The advertiser's address is on the air.** An advertising frame is now
   the header's first octet (type 0 and the TxAdd bit from the
   controller's descriptor), the six octets of address, then the
   advertising data, as an ADV_IND PDU's payload is. It amends ADR 0046.
4. **The central pairs** (`tools/ble_central.py pair`, `--passkey`):
   Security Manager over L2CAP channel 6, LE Secure Connections passkey
   entry as a keyboard-only initiator (AuthReq bonding, MITM, SC; no keys
   distributed) — P-256 keys, twenty rounds of confirm and random values,
   the DHKey checks — then the link layer's encryption start (LL_ENC_REQ,
   LL_START_ENC_RSP) with the LTK the pairing derived, and AES-CCM on its
   own packets. The cryptography is `tools/ble_crypto.py`, checked against
   FIPS-197, RFC 4493, RFC 3610 and the specification's sample data.
   `tools/mesh_api.py bluetooth-pairing fixed-pin --pin N` sets the PIN.

## Consequences

**Good**

- Meshtastic is read over Bluetooth on its default security, as a phone
  reads it: paired with the PIN, every packet after that encrypted on the
  air and checked by its MIC in both directions.
- The core's AES unit answers HCI_LE_Encrypt and the controller's own
  requests, whatever the host.

**Costs, accepted**

- Only the board as peripheral is modelled; a board that is a central would
  need the direction bits the other way.
- A MIC failure drops the packet instead of ending the link with reason
  0x3D, as the controller would on the silicon; no test meets one.
- RANDOM_PIN shows its PIN on the screen; the test sets a fixed one, since
  reading six digits off the panel would test the font, not the pairing.
- Legacy pairing is not implemented by the central; Meshtastic asks for
  Secure Connections.
- Pure-Python P-256 and AES make a pairing take a few seconds of wall time,
  on top of `-icount`'s pace.
