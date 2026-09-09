# ADR 0048 — The connection is made and unmade at once

**Status:** Accepted — 2026-09-09. Corrects ADR 0047. Its own first version,
written earlier the same day and titled "The connection is refused, not
unserviced", was wrong; why it was wrong is recorded below because the mistake
is repeatable.

## Context

ADR 0047 said a central connects and the controller then never services the
connection, and named the status an event ends with as the likeliest cause.
Both halves needed checking.

**The status is not the cause.** `r_sch_prog_end_isr` (ROM `0x4002d58c`) takes
the status from bits five to three, subtracts three, and refuses anything three
or more -- so three, four and five all go on to the dispatch. Status four does
not skip the call; it only clears a flag passed as an argument. A run confirmed
the model already writes three: four hundred and sixty-one ended, one skipped.
No value of that byte could have changed anything.

**Nor is the callback missing.** The handler skips the call outright on a null
pointer, so an empty slot would have explained everything. Read at run time,
through a hatch added for it, every slot is populated -- and every slot holds
`r_lld_adv_frm_cbk`, including on the event kind ADR 0047 read as a connection.
That kind, `0x3002`, keeps the advertising cadence exactly, is byte-identical
to an advertising entry apart from the field that names the kind, and was seen
once before any request had been sent. It is an advertising event.

### The wrong turn

From there the first version of this ADR concluded that no connection forms at
all. It reached that by tracing with a filter set to the address range of the
`r_lld_con_*` functions, and seeing `r_lld_con_cleanup` immediately after
`r_lld_con_start`. The two are not adjacent. Everything between them was
outside the filter, and so invisible; a narrow filter does not merely hide
detail, it fabricates adjacency.

Widening the filter to the whole of the ROM's Bluetooth entry table -- eight
hundred and eighty of them -- and diffing a board that was sent a request
against one left alone answers it without inference. Forty-eight entry points
are woken only by the request, and in this order:

    connect_ind -> adv_end -> llc_start -> con_start
       -> sch_slice_per_add -> sch_slice_compute
       -> con_cleanup -> sch_slice_per_remove
       -> conhdl_register -> hci_build_le_evt
       -> llc_proc_err_ind -> llc_stop -> disconnect_end
       -> llm_link_disc -> conhdl_unregister

So a connection **is** made. The controller stops advertising, starts the link
layer controller, starts the connection, and asks the scheduler to make room
for it. Then it unmakes it: the connection is cleaned up, the slice it had just
been given is taken back, the host is told, an error is raised into the
procedure layer, and the link is discontinued.

**It is immediate.** All of it falls between two advertising kicks, so the whole
life of the link is shorter than one advertising interval. It is not the
supervision timeout ADR 0047 supposed, and no connection event is serviced in
between -- which is why `r_lld_con_frm_cbk`, `r_lld_con_frm_isr` and
`r_lld_con_rx` never run. There is nothing left to run them for.

**It is not the request's shape.** Six boards were each sent a differently
formed request -- window offset nought, one and eight, interval at its minimum
of six and at eighty, and a one-slot window. Every one reached the link layer,
and every one ended the same way.

## Decision

1. **The last thing before the undoing is the scheduler.** `sch_slice_per_add`
   and `sch_slice_compute` run, and then the connection is cleaned up and the
   slice removed. That is where to look next, and it is a much smaller place
   than "somewhere in the connection".

2. **Trace wide, then narrow.** A filter is set from a guess about which code
   matters, so a trace taken through one cannot correct that guess. The
   diff-two-boards measurement costs two runs and is not open to this failure,
   because the reference board is the one that answers what is normal.

3. **Keep the scheduler table readable.** `BOARDEMUL_BLE_SCHED` logs the slot
   the end handler dispatches through, at both ends of an event.

## Consequences

**Good**

- The wall is located precisely: not the end status, not the dispatch, not the
  request's timing, and not a timeout, but whatever the scheduler decides when
  asked to make room for the connection's periodic slice.
- The host is reached. `r_hci_build_le_evt` runs, so the firmware's own stack is
  told about this, and the next step can be checked from the host's side too.

**Costs, accepted**

- ADR 0047 stands corrected twice over, and this document was itself wrong
  once. What survives from 0047 is what ADR 0046 established: the request
  reaches the link layer and the ROM validates it against the specification's
  ranges.
- The event-kind constant the connection test asserts on is still a useful
  signal -- a board left alone scheduled none in four hundred and sixty-six
  events -- but it is not proof of a connection, and one was seen without a
  request. The test keeps it and says so.

## Alternatives rejected

**Leave the first version standing and correct it in a later ADR.** Its title
and its conclusion were both false, and the test it renamed carried the claim
forward. A record that has to be read to the end to avoid being misled is worse
than one that is fixed.

**Keep varying the status anyway.** The dispatcher's own instructions rule it
out; re-measuring it would answer a settled question with weaker evidence.
