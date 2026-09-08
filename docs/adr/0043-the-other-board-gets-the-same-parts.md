# ADR 0043 — The other board gets the same parts

**Status:** Accepted — 2026-09-08.

## Context

Everything built for the T-Deck since the bus learned to route by chip select
— the card (ADR 0033), the radio (ADR 0034), the air and what it does to a
packet (ADRs 0036-0039) — went to that board alone. The Max kept a single
device on SPI2, its e-paper panel, and so kept the same defect ADR 0032
describes: its card and its radio were not unmodelled, they were unreachable.

Its firmware wants both. The binary carries `SD Card Size`, `[SD CARD] Card
Mount Failed`, `[LORA] Starting to listen`, `[SX1262] Received packet!` and the
string `SX1261` — the same identity the T-Deck's radio had to answer with
(ADR 0034). On a run it failed to mount a card six times.

The obstacle was that nothing named the Max's chip selects. The T-Deck's three
had been written down long before anything used them; here only the panel's was
known.

## Decision

1. **The chip selects were read off the firmware, not guessed.** A trace hatch
   on the GPIO block (`BOARDEMUL_GPIO_TRACE`) reports every line the guest
   drives. Pin 8 is pulled low immediately before the burst of SPI that ends in
   the failed mount; the panel's 34 and its data/command 35 alternate
   throughout, as they should. That is ADR 0006's method applied to a pin
   instead of a register.

2. **The Max carries the same three parts on SPI2**: panel on 34, radio on 9,
   card on 8, with MISO per device because the panel answers nothing and the
   other two do.

3. **It takes the same air arguments** — `--air`, `--at`, `--environment`,
   `--antenna`, `--obstruction` — through the same board entry point, so a
   scenario does not have to know which board it is describing.

## Consequences

**Good**

- The Max mounts its card: six failures on a boot became none, which is the
  firmware confirming the pin rather than this record asserting it.
- Its slow tests still pass, and it can join an air and be given a position.

**Costs, accepted**

- **The radio's select is inferred, not confirmed.** Pin 9 is what the T-Deck
  uses and there is an early burst of SPI around it on this board too, but the
  firmware never addressed the radio in any boot measured here — it reached
  zero commands — because LoRa on this build is behind a menu. So the card's
  pin is evidence and the radio's is a reasonable guess wearing the same
  clothes. If a firmware ever talks to it and gets nothing, this is the line to
  doubt first.
- The radio's interrupt line is the T-Deck's number for the same reason, and
  nothing depends on it until a firmware waits on it.

## Alternatives rejected

**Copy the T-Deck's pins wholesale.** It would have been right about the radio
by luck and wrong about the card — the T-Deck's is 39, this board's is 8 — and
nothing would have said which was which.

**Drive the firmware's menu until it touches the radio.** It would settle the
open question above and needs the Max's own interface driven by touch, which is
a piece of work in its own right rather than a step in this one.
