# ADR 0101 — The interrupt matrix remembers its inputs

**Status:** Accepted — 2026-10-01. Found while bringing up the USB serial port
(ADR 0100). This is a carried patch to QEMU's own code, so ADR 0017 applies.

## Context

With the USB serial port modelled, WireScan still printed nothing of its own.
Its `Serial` is Arduino's `HWCDC`, whose `operator bool` becomes true only
inside the port's interrupt handler. That handler never ran.

`HWCDC::begin()` enables the port's interrupt and *then* calls
`esp_intr_alloc`, which routes source 96 to a CPU interrupt. The source was
already asserted when it was routed: the IN FIFO was empty, and a host is
attached. On the chip that makes no difference. The matrix is wiring: each CPU
interrupt line is the OR of every source mapped to it, at every moment.

The pinned fork's matrix, `hw/xtensa/esp32s3_intc.c`, is not wiring. It
forwards a source's level to the CPU line its map entry names *at the moment
the level changes*, and it remembers nothing else. Three things follow:

1. **A source already asserted when it is routed is never delivered**, until
   it happens to change. That is HWCDC's case. It is the normal order for any
   driver that enables its peripheral before allocating the interrupt.
2. **Sources sharing a CPU interrupt clear each other.** ESP-IDF supports
   `ESP_INTR_FLAG_SHARED`. One source going low lowers the line while another
   on it is still high, and that interrupt is lost.
3. **A remapped source leaves its old line where it was.** An asserted source
   moved to another CPU interrupt keeps the first one high.

The fork's own ESP32-C3 matrix (`hw/riscv/esp32c3_intmatrix.c`) does none of
these. It keeps a mirror of every input's level, computes each line as the OR
of its sources, and refreshes the lines a map write touches. That is the
behaviour the S3 needs too.

## Decision

1. **`qemu-patch/intmatrix-level-mirror.patch`**, carried like the others and
   applied by `tools/build_qemu.sh`:
   - the matrix keeps the level of every input, whether or not it is routed;
   - each CPU line is the OR of the inputs mapped to it, recomputed when an
     input changes and when a map entry is written, for the old line and the
     new one;
   - a line is driven only when that OR changes. Some of the S3's CPU
     interrupts are edge-triggered, and QEMU's Xtensa core latches an edge on
     every `set_irq(1)`. Re-driving a line that is already high would invent
     an edge that the chip's OR gate never makes.
   - a reset of the matrix clears its map but not the input mirror. The inputs
     are wires from other devices, and they keep their level through it.

2. **Upstream already has it.** By the time this was ready to report, another
   user had filed the same defect as espressif/qemu#184 (2026-09-30) and
   offered a fix as #185. That fix makes the same choices this patch does,
   including forwarding only changes for the edge-triggered lines. So nothing
   was filed twice. Our independent reproduction, HWCDC's enable-then-route
   order, was added to #184 as a comment. The patch is carried until #185
   lands and the pinned tag moves past it; then it is deleted and this ADR
   superseded, as ADR 0017 says.

## Consequences

**Good**

- A driver that enables its interrupt before routing it works, as on the chip.
- Shared interrupts work, and a remapped source moves cleanly.

**Costs, accepted**

- A sixth carried patch. It touches two files of QEMU's own code, the matrix
  and its header.
- Each input change now scans the map for the line's other sources: 512
  entries per core, on an event that is already a call out of a device model.
