# ADR 0116 — The L76K keeps the board's time, and talks from power-on

**Status:** Accepted — 2026-10-02. Withdraws ADR 0052's "nothing is said until
the firmware has spoken"; narrows its "only two sentences are sent".

## Context

The T-Deck's L76K receiver (ADR 0052) times its reports by the host's clock,
and its own comment says why it then had to stay quiet: "it has no way to see
the guest's clock, so a fix a second on the host's is a flood on a guest
running slower". So it says nothing until the firmware speaks, which a real
module does not do.

ADR 0104 gave the parts a way to see the guest's clock, and the T-Deck Max's
u-blox (ADR 0105) already streams from power-on by it. The L76K was named
there as the next part to follow.

Quectel's *L76K GNSS Protocol Specification* V1.1 (`logs/ref/l76k_protocol.*`)
states what the part does out of the box, and the model differs from it in
more than the clock:

- **Table 17, default configuration:** 9600 bps, one fix a second, and the
  standard messages RMC, GGA, GSV, GSA, VTG, GLL, TXT and ZDA, from GPS and
  BeiDou.
- **Section 2.2, each sentence with an example:**

      $GNRMC,071556.000,A,3149.29103,N,11706.92916,E,0.00,0.00,250420,,,A,V*09
      $GNGGA,071556.000,3149.29103,N,11706.92916,E,1,21,0.7,75.7,M,-5.0,M,,*69
      $GNVTG,0.00,T,,M,0.00,N,0.00,K,A*23
      $GNGLL,3149.29103,N,11706.92916,E,071556.000,A,A*45
      $GNZDA,053712.000,21,10,2021,00,00*49

  The time has three decimals and the minutes five; the model sends two and
  four. RMC ends with a mode indicator *and* a navigational status; the model
  stops after the mode. Talker `GN` is the combined solution, which two
  constellations give.
- **Section 2.3.3, `$PCAS03`:** each field is "output once every N (1-9)
  position fix", 0 is none, and "Null means keeping the previous
  configuration". The fields are GGA, GLL, GSA, GSV, RMC, VTG, ZDA and ANT. The
  model reads two of them and turns an empty field off.

## Decision

1. **The L76K keeps the board's time.** It takes a `clock=` as the u-blox does,
   and the T-Deck hands it the guest clock it already builds. A part with no
   clock, which only a test builds, keeps the host's.

2. **It talks from power-on.** The flood that kept it quiet is gone, so it
   streams as Table 17 says: the first report at one second of the board's
   time, then one every second. A report is stamped with its own instant, the
   stated epoch plus a whole number of the board's seconds, not whenever
   Python got round to it. So the stamp reads `.000`, as the specification's
   do, and is the same in every run.

3. **By default it sends the five standard messages that a position and a
   time decide:** GGA, GLL, RMC, VTG and ZDA. They go in the order `$PCAS03`
   lists them; the specification does not give an order.
   - GSA and GSV report satellites: which were used and where each one is in
     the sky. The model has no sky. Inventing one would put numbers on the
     line that nothing stands behind, so they stay unsent, as ADR 0052 already
     accepted.
   - TXT is in the default set, but the specification's only antenna wording
     is `ANTENNA OPEN`, a fault. What a healthy antenna reports is not given,
     so no periodic TXT is sent. The answer to `$PCAS06` is a TXT and is
     unchanged.

4. **Each sentence has the specification's form:**
   - talker `GN`;
   - UTC as `hhmmss.sss`;
   - latitude `ddmm.mmmmm`, longitude `dddmm.mmmmm`;
   - RMC ending `A,V` (autonomous, navigational status not valid);
   - GLL ending `A,A`;
   - VTG standing still, with magnetic course empty because it is "Not
     supported";
   - ZDA's local zone `00,00`, also "Not supported".

   GGA's satellites in use (9), HDOP (0.9) and geoid separation stay ADR
   0052's stated values. They describe the scenario, not a claim about the
   specification.

5. **`$PCAS03` is read as the specification defines it.** All eight fields
   are rates: once every N fixes, 0 for never, and an empty field keeps what
   was set. A rate for GSA, GSV or ANT is kept, so it reads back correctly,
   but sends nothing, per decision 3.

## Consequences

**Good**

- The firmware's first step, "Wait stop", now meets a line that is talking,
  as it does on a board: it has to silence the module and drain what came
  before. `tests/test_gps_boot.py` is that verdict.
- A firmware that never configures the module, and just listens, hears it.
- The sentences match the part's published examples field for field. Under
  `--icount` their contents follow the guest clock, so they are the same in
  every run.

**Costs, accepted**

- The bytes still reach the UART when the host's serial thread sends them.
  That is up to one guest-clock tick (10 ms) plus a 50 ms poll after the
  stamped instant. Under `--icount` that delivery instant is not yet
  deterministic, as no Python serial part's is.
- The module still always has a fix (ADR 0052). There are no satellites in
  view and no DOP sentence, and no antenna status.
- The u-blox model shares the coordinate formatter and still writes four
  decimals of minutes. u-blox's own interface description would decide that,
  and it is not to hand, so that model is unchanged.

## Alternatives rejected

**Keep the silence and only change the clock.** That fixes the timestamps but
keeps a behaviour the part does not have, and it hides the one thing the
firmware's first step exists to handle.

**Send GSA and GSV from the specification's example sky.** The examples are
not one sky: GSA's example uses satellite 10, which its GPS GSV example does
not list. A sky would have to be made up, and nothing here reads it.
