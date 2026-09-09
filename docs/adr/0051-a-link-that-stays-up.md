# ADR 0051 — A link that stays up

**Status:** Accepted — 2026-09-09.

## Context

ADR 0050 said a connection event does not find its packet by walking the
descriptor ring, and named a per-connection control block in exchange memory as
the next thing to read. That was half right. Reading it, and the four scheduler
interrupt handlers around it, turned up three separate things missing from this
model, each of which alone was enough to keep every packet invisible.

Reading them meant building a disassembler that follows branches instead of
running off the end of a function (`logs/dbg/disas_walk.py`) and a small
symbolic tracker for the address arithmetic (`con_block_offsets.py`), because
the offsets are built as `page + link * 90 + field` through three steps and a
stack slot, and counting them by hand is how the last two mistakes happened.

**One: nothing raised the receive interrupt.** The core signalled two things
about an event, its end and its being skipped. `r_rwble_isr` (ROM `0x4002b564`)
dispatches five bits through a table filled at start-up; read on a running
guest, bit 5 is the end and bit 6 the skip -- the two this model knew -- while
**bit 2 is `r_sch_prog_rx_isr`** and bit 1 is the transmit counterpart. The
receive handler calls the event's own callback with a reason of **2**, and that
reason is an index into a jump table in `r_lld_con_frm_cbk`. Reasons nought and
one, the only ones an ending event produces, lead somewhere else entirely.
Advertising never needed this: its end callback walks the descriptor ring
itself, which is why a connection request arrived perfectly well and nothing
after it did.

**Two: a descriptor did not say which link its packet was for.**
`r_lld_rxdesc_check` reads bits 15..11 of the descriptor's `+12` and compares
them against the link it was asked about; when they differ it reports that
nothing was received, and the whole receive loop in `r_lld_con_rx` is gated on
that answer. The low ten bits of the same halfword are the position within the
half-slot, which ADR 0049 had already read out of the connect handler -- so the
field was half known and half zero. The board's connection is link **one**;
everything this model delivered claimed to be link nought, and was invisible.

**Three: an event did not say which link it was.** The link had to come from
somewhere, and it is in the entry. `r_sch_prog_ble_push` (ROM `0x4002d674`)
writes the event's control structure into the entry at `+8` as halfwords:
`link * 45 + 512`, which is `(link * 90 + 1024) / 2`. So an entry names its own
structure and the link falls out of it -- 512 for advertising, 557 for the
connection, which is link one. Nothing had to be assumed about which activity
an event belongs to.

## Decision

1. **An event that took a packet says so.** The receive bit is raised with the
   end, in one status word, because `r_rwble_isr` takes them in that order and
   the receive handler insists the entry's status is already past two. A
   skipped event received nothing, whatever is in the ring.

2. **A delivered packet names its activity**, taken from the entry being run
   rather than assumed. Advertising is activity nought, so it is unaffected,
   and a second connection would work without changing anything.

3. **A packet arrives when its event opens.** The timestamp was the moment the
   model handed the packet over, which is about two milliseconds before the
   event starts. A connection resynchronises to it every event, so that error
   compounded: the link ran a millisecond fast, at 28.9 ms against the 30 ms it
   had agreed. Timed from the event's own start it runs at 29.89.

4. **What is written is what was read.** The control block has ninety bytes and
   sixteen fields the ROM writes at connection start; this model writes none of
   them, and the one byte it does write -- the event status the hardware owns --
   is written because `r_lld_con_frm_isr` reads it and nothing else ever writes
   it.

## Consequences

**Good**

- **The link stays up.** Fed an empty data packet, the controller runs
  connection events for as long as its master keeps speaking: four hundred and
  two of them over twelve seconds, where before it managed five over a
  hundred and twenty milliseconds and then disconnected. `r_llc_stop` and
  `r_llm_link_disc` do not run at all.
- **It keeps the interval it agreed.** Three hundred and ninety of four hundred
  and two gaps are within a millisecond of thirty; the median is 29.89. The
  dozen that are not are single missed events, sixty milliseconds apart, where
  the medium had no packet ready -- which is a link working, not failing.
- `r_lld_con_rx`, `r_lld_con_rx_sync_time_update` and
  `r_lld_con_rx_link_info_check` all run, four hundred and eighty-two times
  each, having never run once.
- The test asserts the schedule rather than the presence of a constant: that a
  connection outlives the window the specification gives it to establish
  itself, and that its events are an interval apart.

**Costs, accepted**

- **Nothing is carried over the link yet.** The packets are empty ones, which
  is what a master sends when it has nothing to say. `r_lld_con_rx_llcp_check`
  never runs and `r_lld_con_data_tx` never runs, because nothing has asked the
  peripheral for anything and it has nothing to answer.
- The medium has no notion of a channel, so every packet reaches every event
  regardless of hopping, and a packet that misses its event is simply late
  rather than lost on the right channel at the wrong time.
- The event kinds the test treats as advertising are two observed constants.
  The connection's are not enumerated -- anything else is taken as the
  connection's -- because three were seen and there is no reason to think that
  is all of them.

## Alternatives rejected

**Fill the control block until the receive path ran.** ADR 0050 refused this
and was right to: none of the three things actually missing were in that block,
and every hour spent inventing its fields would have been spent on the wrong
structure.

**Take the link from a constant.** One connection would have worked and a
second would have been silently wrong. The entry says which activity it is,
and reading it costs two lines.
