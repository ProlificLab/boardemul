# ADR 0049 — A received packet says when it arrived

**Status:** Accepted — 2026-09-09.

## Context

ADR 0048 established that the controller makes a connection and unmakes it in
the same breath, inside `r_lld_con_start`, and pointed at the scheduler as the
place to look next because `sch_slice_per_add` and `sch_slice_compute` were the
last named things to run before the cleanup. That pointer was wrong; the cause
was upstream of it, in what the model had never told the controller.

`r_lld_adv_pkt_rx_connect_post` runs between the connection request arriving
and the connection being started, and what it does is read the receive
descriptor for the moment the packet landed:

- **`+8` and `+10`** hold a clock, low half then high. The ROM tests the high
  half against `0xfffff000` and raises an internal error if anything above
  twenty-eight bits is set -- the same twenty-eight bit half-slot count the
  exchange table's entries are timed in, and the same one `SLOTCLK` reports.
- **`+12`** is the position within that half-slot. The ROM reads it as
  `624 - fine`, which puts it in half-microseconds, the units everything else
  in this device is in.
- **bits 15..14 of `+6`** select the rate, and index a table of per-rate
  corrections applied to the timestamp.

The model wrote a descriptor's status, its error word, its packet type and the
packet's bytes, and nothing else. So the connection's anchor point was computed
from whatever the previous user of that descriptor had left at those offsets.
The controller did exactly the right thing with it: `r_lld_con_start`'s tail
walks the anchor forward one connection interval at a time until it is ahead of
the current clock, counting events as it goes, and then rejects the connection
if that count is already past its threshold. A nonsense anchor is an
already-expired connection.

This also explains why ADR 0048's sweep found nothing. Varying the window
offset, the interval and the window size cannot rescue an anchor whose *base*
is meaningless, so all six shapes failing was the expected result of this
cause -- not evidence against it.

## Decision

1. **Delivery records when the packet arrived.** The clock and the position
   within the half-slot come from the same virtual clock the exchange table's
   events are scheduled on, so the timestamp is consistent with everything else
   the controller is told.

2. **The rate is written, not left.** It is two bits of a field the model
   otherwise ignores, and a stale value there picks a different correction. A
   field the hardware always sets is one this model must always set.

3. **The offsets are the ROM's.** Every one of them was read out of
   `r_lld_adv_pkt_rx_connect_post`'s own instructions, including the
   twenty-eight bit limit, which is checked there rather than documented.

## Consequences

**Good**

- **The connection lives and is serviced.** `r_lld_con_evt_start_cbk`,
  `r_lld_con_frm_cbk` and `r_lld_con_frm_isr` run, having never run once
  before, and the controller schedules a kind of event it had never scheduled:
  five of them, thirty milliseconds apart, which is exactly the interval the
  request asked for. The link is on its own schedule, not the advertising one.
- ADR 0047's original guess is vindicated in the end: the link now ends because
  the peripheral hears nothing from the master, after about six intervals,
  which is what the specification gives a connection to establish itself.

**Costs, accepted**

- **Nothing is carried over the link yet.** `r_lld_con_rx` and
  `r_lld_con_data_tx` still never run, and correctly so: the central on the air
  sends nothing after its request. Data would need channel hopping, the access
  address and a CRC, none of which the medium has.
- The timestamp is the moment of delivery, which is when the model hands the
  packet over rather than when a radio would have finished receiving it. The
  difference is under a packet's airtime and the ROM applies its own per-rate
  correction on top, but it is an approximation and it is one this device would
  not make.
- The rate is written as one megabit because that is what advertising uses. A
  firmware advertising on the coded or two-megabit PHY would need this to
  follow the event rather than be assumed.

## Alternatives rejected

**Leave the descriptor's other fields alone and work around the rejection.**
There was nothing to work around: the controller was behaving correctly on the
information it had been given. The defect was the model's silence, not the
ROM's judgement.

**Find the offsets by writing plausible values and seeing what sticks.** Faster
to try and impossible to trust; the ROM says where they are and what they mean,
including a bound that a lucky guess would have satisfied by accident.
