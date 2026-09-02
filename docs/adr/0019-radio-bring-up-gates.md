# ADR 0019 — Answer the radio's four bring-up waits, each read off its own loop

**Status:** Accepted — 2026-09-02. Supersedes ADR 0018.

## Context

ADR 0018 left the radio unmodelled: the vendor firmware stopped inside
`esp_wifi_start()` polling an undocumented register, four blind answers to it
changed nothing, and modelling RF calibration against a closed blob looked
open-ended. The user said we could not give up. The reframe that made it
tractable: WiFi does not have to *work*. `esp_wifi_start()` has to *return*,
because `setupUI()` is the second-to-last line of the vendor's `setup()` and
nothing draws until it runs.

The method was the one that had already cracked the GDMA interrupt crash:
never guess at a register, find the code that polls it and read the exit
condition. A one-line hatch in our bus bridge logs the guest PC on each read of
a watched address (`BOARDEMUL_PC_WATCH`, addresses or `lo-hi` ranges); a
filtered `in_asm` disassembles the loop at that PC; the literal pool -- the ROM
image the fork ships, or the app segment in our own flash image -- gives the
register and the mask. Four waits, in the order the firmware meets them:

| # | Register       | Waits for              | Where it was read                          |
|---|----------------|------------------------|--------------------------------------------|
| 1 | `0x6000E050`   | bits 26..24 == 7       | ROM `0x40036a49`: `bnei extui(a8,24,3), 7` |
| 2 | `0x6000E04C`   | bit 24                 | app `0x420da611`: `bnone a12, 0x01000000`  |
| 3 | `0x60006174`   | bit 16                 | ROM `0x40036606`: `bnone a3, 0x00010000`   |
| 4 | `0x60033D14`   | bit 0                  | app `0x420d4832`: `bbci a3, 0`             |

Gates 1 and 2 were found by a turn whose context was later lost; they survived
only as an uncommitted class and its docstring, which also declared the block
after them unclimbable -- "no static answer is correct". Re-measured, that block
was one more status poll (gate 3). After it the firmware ran a calibration sweep
of about 1,075 iterations, and the decisive test was that a 30-second run and a
90-second run gave the same counts: a plateau, not a spin. The sweep finishes.
What followed was the MAC reset handshake (gate 4): write bit 1, wait for bit 0.

Each is a handshake with hardware that is not there: a state machine reporting
done, a calibration step complete, a reset acknowledged. Gate 2 sits inside a
successive-approximation loop that steps a fixed number of times and only
*steers* on the readback, which is why a static done-bit is enough -- it
converges on a meaningless trim, and nothing downstream cares.

## Decision

1. **`PhyGates`** is a trace window that forces the gate bits high on reads of
   exactly those four addresses and nothing else. One address at a time: the
   same pages carry values early boot reads back, and forcing bits across a
   page hangs the guest at reset.

2. **On by default for the T-Deck.** `BOARDEMUL_PHY_GATES=0` turns it back into
   a plain trace window, which is the investigation mode: the firmware then
   stalls at the first unanswered wait and the PC hatch says which loop.

3. **Pages are gated by content, not by name.** `PhyGates.covers()` decides
   from the table, so a fifth gate can never be added to the table and forgotten
   in the wiring.

4. **The table is pinned under test** to the documented (address, bits, loop),
   so a change is either a transcription error or a new finding, and the ADR
   moves with it.

5. **`0x60035000` is never claimed.** The fork's catch-all serves the hardware
   RNG at `WDEV_RND_REG` (`0x6003507C`), and a trace window over that page
   starved the bootloader of entropy and stopped the boot at `entry`. That is
   ADR 0008's amendment happening again, and it is now a regression test.

## Consequences

**Good**

- `esp_wifi_start()` returns. The firmware runs `setupGPS()`, scans for access
  points, finds none -- correctly, there is no radio -- and goes on to
  `setupUI()`. The vendor's LVGL interface draws: sidebar, wallpaper, and
  "Radio is offline", which is the LoRa radio honestly reporting absent.
- Nothing is invented. Every bit forced high is a bit the firmware's own code
  was shown waiting for.
- The method is now a tool: the PC hatch plus a filtered disassembly turns "the
  firmware is stuck" into a loop that can be read, in under an hour per gate.

**Costs, accepted**

- This is not WiFi. Scans return nothing, associations fail, and calibration
  values are meaningless. It lets the firmware *past* the radio, not *onto* it.
- Four undocumented registers are now answered by an emulator that cannot say
  what they are, only what the firmware wanted from them. That is recorded
  rather than hidden.

**Open, as of this record**

- The interface draws but does not yet consume input. Once it is up, the
  keyboard and touch controller stop being read -- 134 keyboard polls in a run,
  all before the interface appeared, and no touch reports at all. That is the
  next thing to find, by the same method, and it is the last step to the goal.

## Alternatives rejected

**Leave it (ADR 0018).** Right on the evidence it had, wrong on the conclusion:
the "unclimbable" block was one more poll.

**Guess at the registers.** Four blind answers to gate 1 changed nothing, and
that is the general case: a wrong answer to a wait looks exactly like every
other wrong answer. Reading the loop takes less time than guessing twice.

**Model the PHY.** Still a closed blob against undocumented registers, and still
unnecessary: the firmware needed four bits, not a radio.
