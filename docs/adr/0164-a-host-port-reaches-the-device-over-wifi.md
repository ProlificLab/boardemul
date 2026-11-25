# ADR 0164 — A port of the host reaches a TCP port of the device over WiFi

**Status:** Accepted — 2026-11-19. Takes the second road ADR 0158 named;
adds to ADRs 0089 and 0097.

## Context

Meshtastic's own clients (its command line, its web client, its phone apps)
talk to a node over a serial port, over Bluetooth, or over TCP on port 4403
when the node is on WiFi. ADR 0158 found the T-Deck build's serial port is
TinyUSB on the USB OTG controller, which is not modelled, and ADR 0157 that
Bluetooth carries no data. The node's WiFi is modelled down to the frames
and the keys (ADRs 0084–0097): it can join an access point the run
provides, take an address by DHCP, and connect out through the uplink.

What it cannot do is be connected *to*. The LAN's TCP endpoint is always the
passive end (ADR 0089): the firmware opens every connection. A program on
the host has no way in.

Meshtastic's T-Deck interface has its own WiFi settings: the image carries
the field names (`WiFi SSID`, and its translations) beside the code that
joins and the API server.

## Decision

1. **`--wifi-forward HOSTPORT:DEVICEPORT`**, with `--wifi-ap`, and as many
   as wanted. `run.py` listens on HOSTPORT. For each client that connects,
   the access point opens a TCP connection from the gateway, `192.168.4.1`,
   to the station's leased address on DEVICEPORT, and the two are spliced:
   bytes both ways, either end's close passed to the other, a reset from
   the device closing the client's socket.

2. **The transport opens actively too.** `emu/tcp.py` gains RFC 9293's
   SYN-SENT: a SYN with the MSS option, retransmitted by the same timer as
   any segment; a SYN-ACK that acknowledges it establishes the connection;
   an acceptable reset refuses it; a SYN alone is a simultaneous open.

3. **The host side is the uplink's machinery** (ADR 0097): one thread owns
   the listening and the accepted sockets, and nothing in it makes the
   guest wait. A forward does not route the guest's own traffic to the
   internet; that stays `--wifi-uplink`'s, and the two can be used together.

4. **Before the station has an address there is nobody to forward to**: the
   client's connection is accepted and closed at once, and the run's log
   says why. A client tries again once the node has joined.

5. **It listens on loopback**, as ADR 0158's serial port does: in the build
   container on all interfaces, published on the host's loopback only. It
   is a setting a device keeps (ADR 0145).

## Consequences

**Good**

- Any firmware that serves TCP on WiFi, a web page or a debug port, is
  reachable from the host: forward its port and connect to `localhost`.
- For Meshtastic that is its TCP API on 4403, once the node is on the run's
  network and on its classic screen; see the first cost below.

**Costs, accepted**

- **Seen end to end with Meshtastic, 2026-11-19, in two conditions of its
  own.** Through its serial API on the USB OTG port (ADR 0166,
  `tools/mesh_api.py`) a T-Deck node took a WiFi network, saved it and
  restarted; it completed WPA2's four-way handshake with the run's access
  point and took 192.168.4.2 by DHCP; and its TCP API then answered on
  `localhost:4403` through this forward, with its node number and settings.
  - Its colour interface does not serve the API. Under display mode COLOR
    Meshtastic 2.7 starts neither its web server nor its TCP API on WiFi
    (`src/mesh/wifi/WiFiAPClient.cpp`, `onNetworkConnected`), so the forward
    met a reset until the node was set to the classic screen
    (`mesh_api.py … display-mode default`). A real T-Deck does the same.
  - Its first boot on WiFi generates a TLS certificate, 140 to 940 s of
    guest time in the runs made, kept in flash afterwards. A device folder
    (ADR 0133) pays it once.
  - From its first attempt to join to the handshake took about 200 s of
    guest time in every run, without a word in its log or a frame the
    access point saw; why is not yet known.
  The earlier note, that the setting never reached flash from the page's
  interface ("Resync …"), stands for the interface: its link to the main
  loop re-requests the configuration every few seconds of guest time and
  never finishes, on a guest clock that follows the host's.
  - *Measured 2026-11-20, and explained.* The silent stretch is WPA2's key
    derivation: core 0 was in `pbkdf2_sha1` on every sample, feeding the
    SHA unit block by block and by GDMA, some 32,000 operations (the SHA
    model's six rejected reads of address 0 per DMA operation, at the end of
    each descriptor list, counted them). Without `-icount` it took 100 to
    104 guest seconds from WiFi.begin to the first authentication, with a
    1 ms core turn or a 250 µs one alike, so it is not the cores waiting on
    each other. Under `-icount` the same join took 5.3 guest seconds and
    reached the access point at about the same wall time (136 s from start
    against 125 s), which was first read as the host executing the firmware
    at about 15 million instructions a second.
  - *Corrected the same day (ADR 0174):* it was not the CPU. QEMU's thread
    was blocked two thirds of the time writing its log, line by line, into
    the run's folder shared from Windows over 9P, and the DMA operations'
    rejected reads made the log enormous. With QEMU's files kept on local
    disk the join takes 7 to 9 guest seconds from WiFi.begin and 22 s of
    wall time, without `-icount`; under `-icount`, 8 guest seconds in 45 s
    of wall time, about 45 million instructions a second.
  - The interface's loop, measured the same day, was mostly the SD card
    read a byte per exchange (ADR 0172); what is left of it shows behind
    the certificate, whose generation holds the main loop.
  - A device folder whose join failed message 2 every time, from
    experiments the same morning, joined again as a fresh device: a key
    ESP-IDF had cached in its NVS, most likely, not a fault of the air.
  - *Also 2026-11-20:* set on the device's own screen, end to end, as a
    person sets it: the settings gear, the WiFi row, the name and key typed
    with the keyboard each field's icon binds, OK; the node saved it,
    restarted, made its certificate (71 guest seconds that time), joined and
    took 192.168.4.2. `tests/test_meshtastic_wifi_screen.py` checks it up to
    the restart and the next boot starting WiFi. Two things the interface
    asks of a person, and of a test: a tap is sometimes lost (tap again
    until the screen changes), and the home screen's title bar holds a
    battery icon that changes on its own.
- The node has to be on WiFi, which on Meshtastic turns its Bluetooth off.
- One station is forwarded to: the first that holds a lease. A run has one
  device, so its access point has one station.

## Amendment — 2026-11-24: the classic screen no longer sticks on WiFi

Trying the same path through the test library (ADR 0182), the colour
interface undid it. Since ADR 0172 made the card fast, the interface
finishes taking its configuration, and on a node with WiFi configured it
writes the display back as colour a few guest seconds after each change and
at each boot. Read back over the serial API: with the region set and no
network, `display-mode default` holds; with a network set, before or after
it, the node reads colour again, starts its colour interface, and serves
neither its web server nor its TCP API. The 2026-11-19 proof above held
only because the interface never finished its configuration then. The
forward itself is unchanged and its tests pass; reaching Meshtastic's
servers through it now needs the classic screen set some other way, which
is not yet found. The attempt is in `logs/wifi-lib/`.

## Amendment — 2026-11-25: the classic screen holds; Meshtastic answers through the library

The way was a matter of timing, not of another path. After each boot the
colour interface asks its node for the configuration (the log says
"[PacketAPI] Send config: display") and, when it has it, writes its own
display setting back, colour included: in a failed run the serial API's
`display-mode default` landed at 23 s of uptime, before the interface's read
at 26 s, and a second "Set config: Display" at 26 s undid it. Sent after
that read, and after the join -- as a person who waits for the screen to
settle would -- it stays: the node restarts on the classic screen, finds the
certificate it made on the previous boot, joins and says "API server listen
on TCP port 4403". Through the host port a `WiFi(forward=...)` gives it,
that API answers with the node's number, its network and the classic
screen. `tests/test_library_wifi_meshtastic.py` checks it; it waits for
those two lines before the display change.

The web
server on port 80 did not answer in twelve minutes of wall time on the
classic screen. The TCP API is what the apps use, and it is what the test
checks.
