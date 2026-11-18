# ADR 0156 — The page reads both serial ports, and a person can type into them

**Status:** Accepted — 2026-11-18. Adds to ADRs 0100 and 0135.

## Context

The chip has two serial ports a firmware talks through: UART0, which QEMU
models, and the USB Serial/JTAG controller, which is ours (ADR 0100). A run
writes what each carries to a file, `<tag>.uart.log` and `<tag>.usb.log`.

The page's console shows the UART's file alone. Firmware built with "USB CDC
on boot" makes `Serial` the USB port: the Max's factory demo does (ADR 0100),
and its console in the page shows the ROM's lines and then nothing.

Nothing goes the other way. QEMU's `-serial file:` takes no input, and the
USB port's chardev is a plain file. A firmware with a command line on its
serial port, a test sketch that waits for a key, Meshtastic's serial API,
cannot be spoken to from the page or from a script.

Both ports can take input as they stand. The fork's UART reads its chardev
like any QEMU serial port, and our USB device already has a receive path:
`usj_receive` fills the OUT endpoint and raises `SERIAL_OUT_RECV_PKT`. QEMU's
`file` chardev takes an `input-path` beside its output path.

## Decision

1. **Each port reads from a named pipe in the run's directory**, `uart.in`
   and `usb.in`, given to QEMU as the `input-path` of its `file` chardev.
   `run.py` makes them and holds them open for writing for the whole run,
   so QEMU's open does not wait for a writer and its reads never see the
   end of the pipe.

2. **Input reaches them as an input event**, as touches and keys do:
   `{"type": "serial", "port": "uart" | "usb", "text": "..."}` in the run's
   `input.jsonl`. Both boards route it to the port's pipe. A page, a script
   and a test drive a serial port the same way.

3. **The page's console reads either port**: `GET /api/runs/<id>/uart`
   takes `port=usb`, and the console has a switch between the two and a
   line to type into. Enter sends the line and a newline.

## Consequences

**Good**

- The Max's own log is readable in the page, and a firmware's serial
  command line can be used from it.
- A test can type to a firmware the way it taps it.

**Costs, accepted**

- A pipe is a Unix thing. Runs are Unix processes everywhere (ADR 0140), so
  this costs nothing, but a run started natively on Windows would need
  another way.
- What is typed goes to the guest as fast as the port takes it: the USB
  port at its full-speed rate (ADR 0100), the UART as QEMU's serial
  front end delivers it, which is faster than a line's baud rate.
