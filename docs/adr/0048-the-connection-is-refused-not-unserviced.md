# ADR 0048 — The connection is refused, not unserviced

**Status:** Accepted — 2026-09-09. Corrects ADR 0047.

## Context

ADR 0047 said the controller accepts a connection and then never services it,
and pointed at the status an event ends with as the likeliest thing keeping
`r_lld_con_frm_cbk` from running. Both halves of that are wrong, and the way
they were shown to be wrong is worth keeping, because the evidence ADR 0047
rested on looked convincing.

**The end status cannot be the gate.** `r_sch_prog_end_isr` (ROM `0x4002d58c`)
takes the status out of bits five to three, subtracts three and refuses
anything three or more -- so three, four and five all continue. It then loads a
callback from a table in RAM and calls it. Status four does not skip the call;
it only clears a flag passed as an argument. Varying the status could not have
changed anything, and a run confirmed the model already writes three: four
hundred and sixty-one events ended, one skipped.

**The callback is not missing either.** The end handler skips the call outright
when the pointer is null, so a null slot would have explained everything. Read
at run time, through a hatch added for it, every entry's slot is populated --
and every entry's slot holds the same function, `r_lld_adv_frm_cbk`.

That is what unpicks ADR 0047. The event kind it treated as proof of a
connection, `0x3002`, carries the **advertising** callback. Beside that:

- its entry is byte-for-byte identical to an advertising entry apart from the
  one field that distinguishes them,
- it falls exactly on the advertising cadence -- 126250 half-microseconds
  before it and 126250 after, the same interval as every other event,
- and the board keeps advertising straight through it, a hundred and
  thirty-eight more times after the last one. A peripheral that had accepted a
  connection would have stopped.

So the controller is not sitting on a connection it declines to service. It is
advertising, briefly differently.

**Where it actually ends.** `r_lld_con_start` (`0x4001b894`) does run, and runs
to its tail: it allocates the connection, computes its timing, and returns
zero. Its last act is a call through a table into firmware code at
`0x4037b618` -- the controller library's own scheduler, not ROM -- and
`r_lld_con_cleanup` runs inside that call. Of the connection's own functions,
only `con_init`, `con_start` and their arithmetic helpers ever execute;
`r_lld_con_evt_start_cbk`, `r_lld_con_frm_cbk`, `r_lld_con_frm_isr` and
`r_lld_con_rx` never do, and after the first request `con_start` is never
entered again.

**It is not the request's timing.** Six boards were each sent a differently
shaped request -- window offset nought, one and eight, interval at its minimum
of six, at eighty, and a one-slot window. All forty requests reached the link
layer in every case, and every case ended in `r_lld_con_cleanup`. Nothing about
the anchor point the ROM computes is what it objects to.

## Decision

1. **Say what the evidence shows and no more.** The test that asserted the
   controller "accepts and starts a connection" asserted a real, reproducible
   fact -- an event kind that appears only after a request -- but named it
   wrongly. It keeps the assertion and loses the claim.

2. **Keep the scheduler table readable.** `BOARDEMUL_BLE_SCHED` logs the slot
   the end handler dispatches through, at both ends of an event. It is what
   turned "the connection is never serviced" into "there is no connection", and
   the next attempt will need it again.

3. **The wall is named where it is.** Not the status, not the callback, not the
   request's timing: it is whatever the firmware's own scheduler decides at
   `0x4037b618`, which loops against the controller clock and checks a returned
   twelve -- the same code `lld_con_start` returns when it refuses outright.

## Consequences

**Good**

- ADR 0047's headline is corrected while it is cheap to correct. A test that
  says a connection exists would have been built on, and everything built on it
  would have had to be unwound later.
- Three explanations are eliminated with measurements rather than argument, so
  the next attempt starts from a much smaller space.

**Costs, accepted**

- **Less is proven than was claimed.** What stands is ADR 0046's result: a
  packet sent from outside reaches the firmware's link layer, is recognised as
  a connection request, and is validated by the ROM against the specification's
  own ranges. The connection itself does not form.
- The firmware hook's address is this build's. Naming it in an ADR dates the
  document to a firmware version in a way the ROM addresses are not dated.
- Six boards booted to test one field each is a slow way to sweep. It was worth
  it once; a second sweep should drive one board through several requests
  instead.

## Alternatives rejected

**Leave ADR 0047 alone and note the correction in the next one.** The claim is
in its title and in a test's name; a reader who found either would believe it.

**Keep varying the status anyway, to be sure.** The dispatcher's own
instructions rule it out, and re-measuring what the code already says would be
answering a question with less evidence than was already in hand.
