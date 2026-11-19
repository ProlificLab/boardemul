# ADR 0158 — A device's serial port is reachable over TCP, for the host's tools

**Status:** Accepted — 2026-11-19. Adds to ADR 0156.

## Context

ADR 0156 lets a person type a line into either serial port from the page.
Tools on the host cannot use the ports at all, and a serial port is where a
firmware's own tools meet it: a sketch's command line, ESP-IDF's monitor, a
terminal, a script.

Nothing in QEMU has to change to give them one. What the guest sends on a
port lands in the run's `<tag>.uart.log` or `<tag>.usb.log` (ADR 0100), and
what the host sends can go into the port's input pipe (ADR 0156).

**Meshtastic's API is not on these ports.** Meshtastic's client speaks one
stream protocol over a serial port and over TCP, and the first aim of this
record was to let it reach an emulated node. Measured on 2026-11-19, the
T-Deck build answers nothing on either port: its image carries `USBCDC` and
`esp32-hal-tinyusb` and no `HWCDC`. Its `Serial` is TinyUSB's CDC on the
chip's USB OTG controller, a different peripheral from the USB Serial/JTAG
controller of ADR 0100, and it is not modelled. Its console banner
(`//\ E S H T …`, in the image) appears in neither log.

## Decision

1. **`--serial-tcp PORT[:uart|usb]` puts a serial port on a TCP port.** The
   USB Serial/JTAG port by default. `run.py` listens on it; one client at a
   time.
   - What the client sends goes into the port's input pipe, byte for byte.
   - What the guest sends from the moment the client connects goes to the
     client, read from the port's log as it grows.
   - A client that leaves frees the port for the next.

2. **It listens on loopback**, so nothing off the machine reaches it. A run
   in the build container listens inside it on all interfaces, and the
   container publishes the port on the host's loopback only
   (`-p 127.0.0.1:PORT:PORT`).

3. **It is a setting a device keeps** (ADR 0145): a port number names no
   file. The settings sheet has a field for it.

4. **The serial probe's slow test talks to it over TCP**: it sends a line to
   the USB port and reads the firmware's answer back on the same socket.

5. **Meshtastic's API stays out of reach, and the two roads to it are
   named**: the USB OTG controller with a host that enumerates TinyUSB's CDC
   device, or Meshtastic's own TCP API on port 4403 over the emulated WiFi,
   with a port of the host forwarded into the LAN.

## Consequences

**Good**

- A firmware whose console is on UART0 or the USB Serial/JTAG port, which is
  what Arduino and ESP-IDF give by default, is reachable from any tool on
  the host that opens a TCP socket.

**Costs, accepted**

- The bytes come from the log file, polled every 20 ms: a client sees the
  guest's output that much late, and a firmware that floods its port is read
  as fast as the file grows.
- One client at a time, as a serial port has one host.
- Meshtastic's client cannot use it, for the reason above.
