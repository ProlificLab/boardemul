# ADR 0097 — The uplink: a NAT from the LAN to the real internet

**Status:** Accepted — 2026-10-01. The piece ADR 0088 deferred behind a flag:
with `--wifi-uplink`, the station reaches the real internet through the host.
Without the flag nothing leaves the machine, as before.

## Context

The faithful LAN (ADRs 0088–0096) is an island. The gateway answers DHCP, ARP,
DNS for the names it hosts, NTP, and the factory-test service, and refuses the
rest. ADR 0088 deferred reaching past it for two reasons that still hold:
guest traffic leaving the machine is a decision of its own, and anything on the
real internet is not reproducible. So this is opt-in, and the tests that must be
deterministic stay hermetic.

Nothing may block the guest. The peer answers the device synchronously, inside a
QEMU timer callback (ADR 0064), and a DNS lookup or a TCP connect can take
seconds. So all host I/O happens off that path, on a thread of its own, and what
comes back is put on the air on later ticks through the peer's outbox -- one
frame per event, as the channel requires.

## Decision

**The gateway routes.** A packet to the LAN -- the gateway, a broadcast, a
multicast -- is handled locally, as now. A packet to any other address goes out
through the uplink when there is one, and is dropped as unroutable when there
is not. The second half tightens the offline LAN too: until now a packet to a
foreign address was answered by whichever local service owned its port, as if
the gateway were that host.

**UDP is NATed per flow.** Each guest flow (its address and port, the remote
address and port) gets one *connected* host UDP socket. The guest's datagrams
are sent on it, and whatever the remote sends back comes to the guest from that
remote address and port. A connected socket only accepts datagrams from the
address it is connected to, so nothing unsolicited gets in. A flow idle for a
minute is closed.

**TCP is terminated at the gateway and spliced**, as QEMU's slirp does. The
guest's connection is a real `emu/tcp` connection. Its SYN is answered only once
the host's own connect to the destination succeeds; if that fails, the guest
gets a reset, as it would from a refused or unreachable host. Bytes are copied
both ways. The host socket is not read while 64 KiB are already waiting for
the guest's window, so a fast server cannot swamp a slow station. The guest's
FIN shuts the host socket's sending half. The host's end of stream becomes the
gateway's FIN to the guest. A reset from either side resets the other.

**DNS is forwarded; mDNS is not.** The gateway stays the station's resolver, as
a home router is. The LAN's own names (`lilygo.local`) are answered from the
zone. Any other name is looked up through the host's resolver (`getaddrinfo`,
off-thread) and answered with the A records it returns, or NXDOMAIN if the host
says the name does not exist. With the uplink the NTP names are no longer
overridden to the gateway, so the firmware sets its clock from real NTP servers.
A `.local` name is link-local and is never sent to the internet.

**Outbound only.** Only guest-initiated flows exist, and no listening socket is
opened. ICMP is not forwarded: it needs raw sockets and privilege on the host,
and this firmware does not use it.

## Consequences

**Good**

- The station can reach anything the host can: real NTP, real DNS, real
  servers. It is the literal "not captive" of ADR 0088, built on the faithful
  LAN rather than instead of it.
- The guest never waits on the host's network. A slow or dead remote costs
  retransmissions on the guest's own timers, as it would on a real network.
- The offline LAN no longer lets a local service answer for a foreign host.

**Costs, accepted**

- **Nondeterminism and egress, when the flag is on.** Real servers answer when
  and how they do. The fast tests use stand-ins on localhost instead (a UDP
  echo, a TCP echo, the name `localhost`). One slow test uses the real internet
  and skips when the host has none.
- **The guest can reach whatever the host can**, including the host's own LAN
  and its loopback, exactly like slirp. That is what an uplink is, and it is why
  the uplink is opt-in.
- No ICMP, no IPv6, no inbound port forwarding. None is used by the firmware;
  each would be its own decision.

## Verification

Hermetic, on Windows and in the Linux container (select and epoll selectors):
`test_uplink.py` covers the host end against localhost stand-ins -- UDP out and
back on its flow, TCP both ways with the ends shut in order, a refused connect,
a 256 KiB server held to the buffer until taken, and lookups. `test_wifi_ap.py`
covers the peer through its frame path -- a datagram answered from the remote
address, a SYN answered only once the host connect succeeds, a reset from an
unreachable host, a name forwarded and answered a tick later, a `.local` name
never forwarded, and, without the uplink, a foreign address left unanswered.

On the real internet (`test_wifi_uplink.py`, container), the firmware's
`pool.ntp.org` came back through the host as four real pool addresses. Its NTP
request left as `192.168.4.2:54866 -> 54.38.114.34:123`, a real server answered,
and it printed `Got time adjustment from NTP!`. The gateway's own NTP was never
used, and the factory report still reached the LAN's service.

Offline, `test_wifi_connect` and `test_wifi_scan` still pass, and the firmware
sent nothing to a foreign address, so routing changes nothing it does.

## Alternatives rejected

**A TAP interface or a kernel NAT.** Both need privileges and a platform-specific
setup, and neither is Python standard library. Host sockets need neither and
work the same in the container and on Windows.

**Answer the SYN at once and reset if the host connect fails.** It is simpler,
but the guest would see a connection succeed and then die, which no real
unreachable host does.

**Keep answering foreign-addressed packets locally without an uplink.** That is
the captive behavior ADR 0088 set out to remove, just moved from names to
addresses.
