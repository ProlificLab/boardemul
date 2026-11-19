# ADR 0166 — The USB OTG port has a host: a firmware's TinyUSB serial port is read

**Status:** Accepted — 2026-11-19. Takes the first road ADR 0158 named.

## Context

Meshtastic's T-Deck build talks on `Serial`, which in that image is TinyUSB's
CDC device on the ESP32-S3's USB OTG controller (ADR 0158). Its whole log
goes there, and so does its client API: `meshtastic --port`, the web
client over Web Serial and the phone apps over a cable all speak to a node
this way. Nothing in BoardEmul answers at `0x60080000`, so the firmware's
own account of itself has been invisible: on 2026-11-19 a node that kept
light-sleeping and never saved a WiFi setting could only be read from its
registers, a core at a time.

The controller is Synopsys' DWC2 in device mode. Its register map is in
Espressif's `soc/esp32s3/include/soc/usb_reg.h` and `usb_struct.h`; the
firmware's view of it is TinyUSB 0.15.0's `dcd_esp32sx.c`, the driver
Espressif's Arduino build of TinyUSB compiles for the S2 and S3, read from
its published source: what it programs at init and on a bus reset, which
interrupt bits it serves, how it reads the receive FIFO (status 2 OUT data,
3 OUT done, 4 SETUP done, 6 SETUP data) and how it fills the transmit FIFOs
a packet at a time on TXFEMP. Its interrupt is source 38,
`ETS_USB_INTR_SOURCE` in `soc/periph_defs.h`. Where the driver leaves the
core's behaviour to the core, the rules are DWC2's as the same core's
published manuals state them (the OTG_FS chapter of ST's RM0090, which is
this core): the core clears an OUT endpoint's EPENA before it sets SETUP
phase done or transfer completed; an OUT transfer completes on a short
packet or when its size or packet count runs out; a SETUP is always taken.

### The first model, and why it moved

The first model was a Python bus region. It enumerated Meshtastic, and its
log began to flow: the banner, "Booted", the node database. Then it stopped,
after 285 bytes with the 1 ms core turns of ADR 0161 and after 4469 with
100 ms ones. The bus trace showed why. TinyUSB's interrupt handler, on one
core, clears an endpoint's bit in DIEPEMPMSK (`0x834`) with a read and a
write; its task, on the other core, sets one with a read and a write; neither
holds a lock. On silicon each read-modify-write takes a few cycles and the two
never meet. Over the bus bridge each access is a round trip of tens of
microseconds, the handler's pass outlives a core's turn, and the trace showed
the handler read `0x10`, the task write `0x10`, and the handler write `0`:
the task's request for the FIFO-empty interrupt lost, and the CDC write
waiting on it for ever.

The same slowness had already forced a rule nothing on silicon needs: the
handler closes every pass with `gintsts |= <what it does not handle>`, which
clears whatever arrived during the pass, and an ENUMDNE raised inside the
pass that answered USBRST was lost and endpoint 0 never sized. The host had
to wait for the pass to close.

The first seemed to come from a controller that answers in tens of
microseconds what silicon answers in nanoseconds, and moving the controller
into QEMU was meant to end it.
The second also comes from time running differently here than on silicon,
and outlives the move (decision 2). So, it turned out, does the first, for a
reason that is not the controller's speed at all (decision 3).

## Decision

1. **The controller is a QEMU device, `esp-usb-otg`
   (`qemu-patch/esp_usb_otg.c`), at `0x60080000` raising source 38, on both
   boards.** Its registers are served at the speed of any other device:
   - the global, device and per-endpoint registers TinyUSB uses;
   - one receive FIFO of status entries and their data, read through any
     FIFO window;
   - one transmit FIFO per IN endpoint, written through the endpoint's own
     window (window n is "Device EP n FIFO" in `usb_struct.h`; TXFNUM only
     places the FIFO in RAM and matters to a flush by number);
   - the interrupt bits derived as DWC2 derives them: RXFLVL from the
     receive FIFO, TXFE while an endpoint's FIFO is empty, DAINT from the
     endpoint interrupts and their masks (TXFE's being DIEPEMPMSK), IEPINT
     and OEPINT from DAINT and DAINTMSK, the line from GINTSTS and GINTMSK
     under GAHBCFG's global mask.
   - A packet leaves an IN FIFO once it holds one; transfer size and packet
     count count down; the last packet completes the transfer. An OUT
     packet is taken while the endpoint is enabled and its transfer not
     complete, and held (NAKed, to the host) otherwise.

2. **The host is Python, at the other end of a cable**: a character device
   socket carrying packets, not registers. Over it the controller says
   *attached*, *detached*, *ready* (the bus is reset and recovered), *IN
   packet on endpoint n* (with the endpoint's packet size), and *your OUT
   packet on endpoint n was taken*; the host says *SETUP* and *OUT packet on
   endpoint n*. That is the traffic of a USB cable, and nothing on it waits
   for an answer, so a register access never leaves QEMU.
   - The bus's times are the host's and are kept by the controller on the
     guest's clock, as a host's are fixed in the specification: a 100 ms
     attach debounce (TATTDB, USB 2.0 7.1.7.3), a 10 ms reset (TDRST), and
     10 ms of recovery before the first SETUP (TRSTRCY, 9.2.6.2).
   - They are minimums, and each step also waits for the driver to have
     served the one before: the reset ends once the driver has cleared
     USBRST and closed that handler pass (its next write to GINTSTS), and the
     recovery starts once it has done the same with ENUMDNE; a driver that
     never writes again gets 50 ms. On silicon 10 ms is a thousand handler
     passes and these orders hold by themselves. Here the guest's clock is the
     host's unless the run counts instructions, and a loaded host stalls a
     vCPU past 10 ms: six probe runs at once, before this rule, lost five to
     an IntegerDivideByZero in `dcd_edpt_xfer`, the pass that answered USBRST
     having cleared ENUMDNE and left endpoint 0 without a size. After it,
     six of six passed. The rule removes only what silicon cannot produce.
   - The host enumerates as a host does: GET_DESCRIPTOR, SET_ADDRESS, the
     device and configuration descriptors, SET_CONFIGURATION, and for the
     CDC interface it finds there SET_LINE_CODING (115200 8N1) and
     SET_CONTROL_LINE_STATE with DTR and RTS, which Arduino's CDC waits for
     before it sends anything. Then it reads the CDC data IN endpoint and
     writes to its OUT endpoint what a person or a tool typed, a packet at a
     time.
   - With no cable (no character device), the device leaves soft-disconnect
     and nothing answers, as on a board plugged into nothing.

3. **A read-modify-write of one of the controller's registers is kept whole
   across the cores.** Moving the controller into QEMU did not end the
   DIEPEMPMSK race, which Meshtastic still lost within its first few
   kilobytes. A trace of every access with the core that made it (the
   device's `trace` property, off by default) showed why: this QEMU lets
   memory-mapped I/O happen only at the last instruction of a translation
   block (`translator.c` clears `can_do_io` before the first), so every
   register access ends a block, and a core's turn can end between its read
   and its write. The other core then runs a whole turn inside a window that
   on silicon is three cycles: the handler on core 0 read `0x10`, the task on
   core 1 set the bit and wrote `0x10`, core 0 wrote `0`.
   - A core that reads one of the plain registers (not the FIFO, the
     receive status, or a write-one-to-clear register) holds it until its
     next access to the controller. Another core that touches the held
     register meanwhile yields its turn and does the access again, as
     QEMU's own `mips_itu.c` blocks a core inside a register access
     (`cpu_loop_exit_restore` at `mem_io_pc`); the holder is next in the
     round robin and finishes at once. A hold never outlives its holder's
     next access, or 250 ms for a core that reads and goes away.
   - That is the order silicon's timing gives, and only that. A merge after
     the fact cannot recover it: the task's `|= 0x10` on a register already
     at `0x10` changes nothing that can be seen.
   - The abandoned access releases memory.c's re-entrancy guard, which
     otherwise refuses every later access as re-entrant and the guest takes
     a LoadStorePIFAddrError.
   - Measured: Meshtastic, which froze within twelve seconds of boot every
     time, runs; its log flows and its client API answers on the port.

4. **The port is a third serial port, `otg`**, beside the UART and the USB
   Serial/JTAG port (ADR 0156): what the device sends goes to
   `<tag>.otg.log`, the page's console can show it and type into it, and
   `--serial-tcp PORT:otg` puts it on a TCP port (ADR 0158).

5. **What is not modelled, said once in the log when met**: suspend and
   resume, remote wake-up, isochronous transfers, stalls beyond the flags,
   and more than the one CDC function. A host that meets a device it does
   not understand stops enumerating and says what it saw.

## Consequences

**Good**

- Meshtastic's own log is readable, live, from the page.
- Meshtastic's client API is reachable from the host, over the stream
  protocol it speaks on a serial port.
- Any firmware built with TinyUSB's CDC on the S3 gets a working USB
  serial port.
- The driver's unguarded read-modify-writes keep silicon's order across the
  cores (decision 3), where the round robin used to break it.

**Costs, accepted**

- A fourth QEMU device of ours, and a rebuild of the tree to get it.
- The host is ours, not an operating system's: one configuration, one CDC
  function, no hubs. It is what a terminal sees through a cable.
- A core that touches a held register gives up the rest of its turn. The
  holds are short, so the yields should be rare; the first in a run is
  logged, and how many follow is not counted.
- Other devices of ours with registers both cores read-modify-write have the
  same exposure and no hold; none has been seen to lose one. The rule is in
  this device, where it was measured, not in the scheduler.
