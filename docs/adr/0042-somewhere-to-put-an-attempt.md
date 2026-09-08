# ADR 0042 — Somewhere to put an attempt

**Status:** Accepted — 2026-09-08.

## Context

Chasing the crash in ADR 0041 produced a candidate fix: the S3 cache model
writes guest RAM directly through the region pointer and never tells QEMU the
memory changed, so this declared those writes. It was written, built, and
measured against the reproduction — and it made no difference at all, three
faults in fifty-six runs against the same rate before.

So it was reverted, and at that moment the only record that it had ever been
tried was a sentence in a conversation. The next person with the same idea —
including a later me — would have had no way to know it was already ruled out,
and would have spent the same afternoon on it.

That is the gap. A patch one directory up is a *fix*: found, diagnosed,
reported, carried until it lands upstream (ADR 0017). There was nowhere for the
step before that, the change that might be a fix and has to be measured to find
out.

## Decision

1. **`qemu-patch/experiments/` holds candidates**, each with what it was for
   and what measuring it showed — including, especially, when it showed
   nothing.

2. **Nothing there is applied unless named**, one per comma in
   `BOARDEMUL_QEMU_EXPERIMENTS`. The build announces each one it applies and
   says the build is not a plain one.

3. **A build reverts any experiment it was not asked for.** Applying one and
   leaving it in the tree would make every later build silently different,
   which is the undeclared drift ADR 0001 exists to prevent, and it would be
   invisible.

4. **An experiment is not a fix and does not become one quietly.** Anything
   that turns out to be right graduates: up a directory, with an ADR, an issue
   and a pull request.

## Consequences

**Good**

- A negative result is kept, which is the whole point: it stops the same
  ground being covered twice.
- Trying something against the emulator's one dependency stops being a thing
  you do in a working tree and hope to remember.

**Costs, accepted**

- A directory of changes that are, by construction, not believed to be right.
  The README says so of each, and the build refuses to apply any of them
  without being asked by name.
- Someone could still run with an experiment on and forget. The build printing
  "this build is not a plain one" is the guard, and it is a weak one; the
  revert-when-not-asked rule is the stronger half.

## Alternatives rejected

**A branch per attempt in the fork.** The fork exists now and could hold them,
but an attempt against the pinned tag is not the same thing as a change offered
upstream, and mixing the two would make the pull requests harder to read.

**Write it up in the ADR and drop the diff.** Prose describing a patch is not a
patch: the next person has to reconstruct it before they can re-measure, which
is most of the work again.
