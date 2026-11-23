# ADR 0181 — The first public version is 0.1.0

**Status:** Accepted — 2026-11-23. Decided with the product's owner (the
product interview of that day, Q14, Q25, Q28). Supersedes ADR 0162's
numbering; its changelog and its check on every push stand.

## Context

ADR 0162 gave BoardEmul a version, 1.0.0 on 2026-11-19, and five more
followed to 1.5.0 before anything was published. The product's first
public release adds a library and a command whose interfaces will move
while people start using them, and its owner wants the number to say so.

## Decision

1. **The first public release is 0.1.0.** Versions follow semantic
   versioning: before 1.0, the second number moves when something stops
   working as it did, the third for anything else. 1.0 is when the test
   library and `boardemul test` are stable.

2. **The versions before publication become internal**: the changelog
   keeps them, under *Internal versions, before publication*, renamed i1.0
   to i1.5. Between them and 0.1.0 the code carries `0.1.0.dev0`.

3. **A release is a git tag**, `v0.1.0`, from which the CI builds and
   publishes the image and the PyPI package. Each release's changelog names
   the firmware it was tested with. Support is the repository's issues, on
   a best-effort basis, with no promised delay.

## Consequences

**Good**

- A number that tells a user how settled the interfaces are.

**Costs, accepted**

- A changelog whose history counts from i1.0 before it counts from 0.1.0.
