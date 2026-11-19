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
- The node has to be on WiFi, which on Meshtastic turns its Bluetooth off.
  On BoardEmul Bluetooth carries no data anyway (ADR 0157).
- One station is forwarded to: the first that holds a lease. A run has one
  device, so its access point has one station.
