# ADR 0083 — The GDMA could not reach PSRAM, so DMA SHA over a received frame failed

**Status:** Accepted — 2026-09-17. The fix for the defect ADR 0082 located. A
carried QEMU patch under ADR 0017.

## Context

ADR 0082 proved the firmware miscomputes message three's MIC over correct
inputs, and placed the fault in the emulated crypto. Logging the SHA
accelerator (a temporary build) showed exactly how.

The supplicant computes the HMAC through the SHA accelerator's DMA mode: it
points a GDMA descriptor at the message and asks the accelerator to hash N
blocks. For message three's inner hash the driver issues, in order, a one-block
DMA-start, a **two-block** DMA-continue, and a one-block DMA-continue -- four
blocks in three operations. The one-block operations produced a digest; the
two-block one produced none, and the log around it was:

    SHADBG op=3 mode=0 block=2
    Invalid read at addr 0x3D87A894, size 4, region '(null)', reason: rejected
    Invalid read at addr 0x3D87A898, ... rejected   (x16)

The DMA read of the frame at `0x3D87A8xx` was rejected, so
`esp_sha_continue_dma` returned early without hashing those two blocks. The
inner hash covered the ipad and the last block but skipped the two middle
blocks of the frame, giving a wrong digest and a wrong MIC -- exactly what
`tools/dump_final.py` measured.

**Why that address is unreachable.** `0x3D8xxxxx` is the external-memory
(flash/PSRAM) window; the WiFi receive buffer for message three lives in PSRAM.
The GDMA's DMA address space is built over one memory region:

    hw/xtensa/esp32s3.c:  object_property_set_link(&ss->gdma, "soc_mr", OBJECT(dram), ...)
    hw/dma/esp_gdma.c:    address_space_init(&s->dma_as, s->soc_mr, "esp.gdma");

`dram` is the internal SRAM alone (0x3FC80000, 0x170000 bytes). So the GDMA can
DMA to and from internal RAM and nothing else -- not PSRAM, not the external
window. On real silicon the GDMA reaches PSRAM; it is DMA-capable memory and
the WiFi stack puts receive buffers there.

**Why only message three.** Message two is built by the station in internal
DRAM, so its HMAC DMA reads succeed and its MIC is correct -- which is why the
peer always verified message two and the whole schedule looked right. Message
three is *received* into a PSRAM buffer, so its HMAC DMA reads are rejected and
its MIC comes out wrong. The received frame itself was always perfect (ADR
0081); the fault was reading it by DMA, not the frame.

## Decision

**Give the GDMA the SoC's whole address space, so it can DMA from PSRAM as the
hardware does.** One line, carried as `qemu-patch/gdma-reaches-psram.patch`:

    -        object_property_set_link(&ss->gdma, "soc_mr", OBJECT(dram), ...)
    +        object_property_set_link(&ss->gdma, "soc_mr", OBJECT(sys_mem), ...)

`sys_mem` is `get_system_memory()`, already in scope. The GDMA's address space
now covers internal RAM, PSRAM and the rest, which is what a real DMA controller
sees. Filed upstream as espressif/qemu issue 180 and offered as PR 181, like the
other carried patches (ADR 0017); when it lands and the pinned tag moves past
it, delete the patch and supersede this ADR.

## Consequences

**Good**

- **The WPA2 four-way handshake completes, for real, and the station connects.**
  Measured after the fix: the peer receives genuine message fours (Secure bit,
  verified), reports handshakes done, the firmware prints "Connected to access
  point" and raises its connected event, and the station begins sending data
  frames -- ARP and DHCP -- where before it sent nothing and left with reason 8.
  This was the wall from ADR 0058 through 0082.

- It resolves the root cause rather than the symptom. Every finding from ADR
  0067 to 0082 was a true measurement pointing here; the fault was one property
  link, and correcting it makes them all resolve at once.

- No regression: the fast tier passes, and `tests/test_wifi_connect.py` and
  `tests/test_mesh.py` both still pass with the wider address space.

**Costs, accepted**

- **A second carried QEMU patch** (after ADR 0066's), against ADR 0001's wish
  for the smallest possible footprint. It is one line and declared, and it is
  offered upstream so it need not be carried forever.

- **The GDMA can now address MMIO as well as RAM.** A descriptor pointing at a
  device register would reach it, which the `dram`-only space prevented. Real
  drivers point DMA at RAM, and real hardware has the same reach, so this
  matches the hardware rather than diverging from it -- but it is a wider door
  than before.

- **Reaching an IP is still ahead.** The station now sends DHCP, but the Python
  peer answers only EAPOL; the encrypted data frames it now receives ("data
  frame that is not EAPOL") are the next work (ADR 0064's goal), and the MIC/CCMP
  keys are all proven correct to build on.

## Alternatives rejected

**A narrower address space of DRAM plus PSRAM only.** More faithful to what the
GDMA may touch, but more code -- a container region built by hand -- for a
distinction real drivers never exercise. The one-line system-memory link is
smaller and matches the hardware's reach.

**Fix it in the SHA model instead.** The SHA model is correct; it asked the
GDMA for bytes and the GDMA could not fetch them. The fault is the GDMA's reach,
and that is where it is fixed.

**Model the crypto in the peer to sidestep it (ADR 0064's identity idea).** That
was for the data path, where both ends of the air are ours. The handshake MIC
is computed inside the firmware, not on the air; no peer-side modelling can
change what the firmware's own accelerator computes. The accelerator had to be
able to read the frame.
