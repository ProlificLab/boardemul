# ADR 0100 — The USB serial port, with a host on the other end

**Status:** Accepted — 2026-10-01. Found by the hardware census that followed
ADR 0099: boot every firmware, then count what nothing answered.

## Context

Three of the T-Deck-MAX images said almost nothing. `test_Elink_paper` printed
the ROM's lines and then nothing at all for 120 s. `WireScan`, a bus scanner,
printed `i2cInit` and never a scan result. The factory demo went quiet after its
touch controller came up. None of them was stuck: their `Serial` is not UART0.

The ESP32-S3 has a USB Serial/JTAG controller at `0x60038000`. It is a fixed
USB device, a CDC-ACM serial port and a JTAG bridge, wired to the chip's own
USB pins. Arduino's `HWCDC` class drives it, and an image built with "USB CDC
on boot" makes `Serial` an `HWCDC`. WireScan and the factory demo both carry
`HWCDC`, and all four MAX images carry the IDF's `usb_serial_jtag` driver.

The pinned fork maps a placeholder there, `esp32c3_jtag.c`: every read returns
0 and every write is dropped. Whatever a firmware printed through it was lost.
It was also slower than the hardware, not just quieter. `SERIAL_IN_EP_DATA_FREE`
read 0, so the FIFO looked permanently full.

What the controller does is published:

- the register map, fields and reset values in the IDF's
  `soc/esp32s3/include/soc/usb_serial_jtag_reg.h`;
- how the IDF drives it, in `hal/esp32s3/include/hal/usb_serial_jtag_ll.h`:
  bytes go through `EP1` while `SERIAL_IN_EP_DATA_FREE` is set, and `WR_DONE`
  hands the packet to the host; received bytes are read from `EP1` while
  `SERIAL_OUT_EP_DATA_AVAIL` is set;
- how Arduino 2.0.14 (the core these images were built with) uses it, in
  `cores/esp32/HWCDC.cpp`. `begin()` clears every interrupt and then enables
  `SERIAL_IN_EMPTY`, and the first such interrupt is how it learns a host is
  there. Before that its transmit timeout is zero and output is dropped once its
  256-byte ring is full. So with a host attached, `SERIAL_IN_EMPTY` must
  re-assert while the IN FIFO is empty, even after it has been cleared. That is
  also why its ISR masks the interrupt once it has nothing to send.
- its interrupt source, `ETS_USB_SERIAL_JTAG_INTR_SOURCE`, is 96 in
  `soc/esp32s3/include/soc/periph_defs.h`.

## Decision

1. **`qemu-patch/esp_usb_serial_jtag.c`**, a device of our own mapped over the
   placeholder at priority 100, as the other C devices are (ADR 0005). It
   raises source 96 through the interrupt matrix, `INT_ST = INT_RAW & INT_ENA`.

2. **The serial side is modelled; the JTAG side is not.** The JTAG FIFO
   reports empty, as it is at reset. Every register keeps the reset value the
   header gives it, and the read-only ones report the FIFOs' real state.

3. **A host is plugged in and listening**, as on a desk with a serial monitor
   open. It takes a flushed packet after the time a full-speed bus (12 Mbit/s)
   needs to carry it, and only then does the FIFO empty. A FIFO that fills is
   sent without `WR_DONE`. The ROM's own console writer (`0x40049068`) shows
   this: it writes all 64 bytes and then waits for room, and never writes
   `WR_DONE` in between. The first cut of this device left such a packet in
   the FIFO, which cut every long log line at 64 bytes. With the FIFO never
   emptying, `SERIAL_IN_EMPTY` never came, so HWCDC's own output stayed in its
   ring. `SERIAL_IN_EMPTY` is
   set for as long as the IN FIFO is empty. `SOF` is raised each millisecond
   while it is enabled, and `FRAM_NUM` counts milliseconds of guest time. An
   unplugged port is not modelled.

4. **What the host receives goes to a file**, `<tag>.usb.log` beside the UART
   log. What it sends comes from the chardev's input, in packets of up to 64
   bytes, one at a time, each raising `SERIAL_OUT_RECV_PKT`.

5. **`run.py` treats the port as output**, like the UART: the stall watchdog
   counts it as a sign of life, the manifest records its size and path, and the
   end-of-run summary shows its last lines.

## Consequences

**Good**

- Firmware whose `Serial` is the USB port says what it is doing, and its
  `Serial` writes take the time they would on a board plugged into a PC.

**Costs, accepted**

- A firmware that behaves differently with nothing plugged in cannot be shown
  that way yet.
- Another carried device. It is ours, like the others, so it adds no change to
  QEMU's own code.

## Alternatives rejected

**Fix `esp32c3_jtag.c` in the tree.** That edits QEMU's code, not ours. It
would also be a C3 file serving an S3, and ADR 0005 keeps our models out of the
pinned tree.

**Route `HWCDC` to UART0.** That changes what the firmware does, which ADR 0006
rules out.
