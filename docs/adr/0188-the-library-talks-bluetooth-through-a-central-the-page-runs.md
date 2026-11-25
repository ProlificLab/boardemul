# ADR 0188 — The library talks Bluetooth through a central the page runs

**Status:** Accepted — 2026-11-25. Closes the gap ADRs 0180 and 0182 left:
"WiFi and Bluetooth reach the test library after its first version".

## Context

`tools/ble_central.py` is a Bluetooth LE central that joins a run's
Bluetooth air (ADR 0045): it connects, exchanges the MTU, lists services and
characteristics, reads, writes, and pairs with a passkey and encrypts (ADRs
0169, 0171). It has only been driven from the command line and from slow
tests that start their own run.

A test written with the library (ADR 0182) reaches devices through the
page's API and imports nothing of the emulator. The air a central has to
join is wherever the device's run is: beside the device folders on a host
that runs natively, and in the `boardemul-air` Docker volume, on the
`boardemul-air` network, where runs go into the build container (ADRs
0140, 0145) -- out of reach of a library on the host.

## Decision

1. **The page runs the central where the device's run is**: the same
   Python on a native host, a `docker run` of the build image on the airs'
   volume and network otherwise, exactly as a run is placed
   (`emu/device.py`).
2. **One request, a list of operations**: `POST /api/runs/NAME/ble` with
   `{"ops": [...]}`. The central connects to the device (by the air the run
   was started with), does each in order -- `services`, `characteristics`
   of a service, `read` and `write` of a characteristic by its UUID (bytes
   as hex), `pair` with a passkey, which also encrypts -- and answers each
   one's result, or the error that stopped it. It disconnects at the end:
   a request is one connection.
3. **In the library**: `Device.start(ble=True)` puts the device on a
   Bluetooth air of its own name; `Device.ble(*ops)` sends a list, and
   `Device.ble_services()`, `ble_read(uuid)`, `ble_write(uuid, data)` are
   single operations, with `passkey=` to pair first.
4. **On the command line**, `tools/ble_central.py AIR ops --ops JSON` is
   the same thing, and prints the answer as one line of JSON.

## Consequences

**Good**

- A firmware test can check what its device serves over Bluetooth, from
  Python, on any host.

**Costs, accepted**

- A connection per request: a test that wants notifications over a long
  connection is still a script with `tools/ble_central.py`'s `Central`.
- On a host that runs in containers, each request starts a container, a
  second or two before the central is on the air.
