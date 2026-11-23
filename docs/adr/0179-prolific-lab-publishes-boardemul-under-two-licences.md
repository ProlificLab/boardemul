# ADR 0179 — Prolific Lab publishes BoardEmul under two licences

**Status:** Accepted — 2026-11-23. Decided with the product's owner (the
product interview of that day, Q4, Q5, Q7, Q8, Q26).

## Context

BoardEmul is to be a distributable product: people other than its authors
install it and test their firmware with it. It is Prolific Lab's, which
decides its licence and publishes it. Nothing in the repository said under
what terms it could be used.

Part of it is not free to license: the files in `qemu-patch/` are compiled
into QEMU, which is GPLv2, so whoever receives a built QEMU receives them
under the GPL. The rest -- the Python side, the tools, the tests, the
documentation -- is Prolific Lab's to license as it chooses, and the people
it is for include manufacturers and schools, who may build it into their
own tooling.

Vendor firmware is fetched from where its vendors publish it and never
redistributed (ADR 0147); no licence here covers it.

## Decision

1. **Copyright 2026 Prolific Lab.**

2. **Everything but `qemu-patch/` is Apache-2.0**: the root `LICENSE` is its
   text, and the Python files say so on their first lines with an SPDX
   identifier.

3. **`qemu-patch/` is GPL-2.0-or-later**, the licence of the QEMU it is
   built into: `qemu-patch/LICENSE` is GPLv2's text, and each file carries
   the identifier.

4. **`NOTICE` says which is which**, and that vendor firmware is not part of
   either.

5. **Contributions carry a Developer Certificate of Origin**: each commit
   from outside is signed off (`git commit -s`), and the CI checks it.
   `CONTRIBUTING.md` says so, with how this project works (ADRs before code,
   the standard library only, the test tiers).

## Consequences

**Good**

- The terms are stated before anything is published, and a manufacturer
  can use the Python side in its own tooling.
- The QEMU side meets the GPL's terms as QEMU's own code does.

**Costs, accepted**

- Two licences in one repository: a file's licence is the one its
  identifier names, and moving code between `qemu-patch/` and the rest
  changes its licence.
- A DCO certifies a contribution's origin; it does not let Prolific Lab
  relicense it later, as a CLA would.
