# ADR 0147 — A new machine is ready in three commands, and says what it lacks

**Status:** Accepted — 2026-11-18. Amends ADR 0003's manifest.

## Context

Everything a run needs beyond this repository comes from three places:

- the build image, from `.devcontainer/Dockerfile` (ADR 0002);
- QEMU, built into `third_party/qemu` by `tools/build_qemu.sh` (ADR 0138);
- the vendor firmware, which is not committed. `firmware/MANIFEST.toml`
  pins each image by URL, size and SHA-256.

Nothing fetches the firmware. A new user reads the manifest and downloads
each image by hand, and the test suite then skips whatever is missing,
quietly. The manifest itself has drifted:

- **Its URLs name `master`.** LilyGO rewrote the T-Deck repository's
  firmware page on 2026-10-27 (commit 2027dee7), and
  `T-Deck_UnitTest_250620.bin` left `master` that day. That is the build
  most of the T-Deck's slow tests boot, ADR 0019 onward, and it is not in
  the manifest at all. It is still in the repository's history, from
  commit e2aeb7e8 (2025-06-20), at the same 16,711,680 bytes as the copy
  here.
- **The other seven are on `master` today**, at the sizes pinned. Each was
  last changed by one commit. A URL naming that commit keeps serving those
  bytes after the next rewrite.
- **The probes are not vendor images** (`tools/*_probe`), and the
  manifest's header names only the first of the four.

And when something is missing, nothing says what. A run on Windows without
Docker running, or without the image built, fails with Docker's own error
(ADR 0140). A missing QEMU fails with a path.

## Decision

1. **`tools/fetch_firmware.py` fetches what the manifest pins**, and only
   that:
   - each image from its URL, or a member of a zip archive when the
     manifest names `archive` and `member`;
   - into `firmware/<path>`, through a `.part` file renamed only once the
     size and SHA-256 match, so a failed download never leaves a file a run
     would trust;
   - an image already present with the right hash is not fetched again, and
     one present with the wrong hash is reported and left alone;
   - `--check` fetches nothing and reports each image;
   - `--only NAME` takes one image.

2. **The manifest's URLs name commits, not `master`.** Each points at the
   last commit that changed its file. `T-Deck_UnitTest_250620.bin` is added,
   at e2aeb7e8. The header names all the probes as built, not fetched.

3. **`tools/doctor.py` says what this machine lacks, and the command that
   fixes it.** It checks, in order:
   - Python 3.11 or later, for `tomllib`;
   - where a run will start (ADR 0140), and for a container run: Docker on
     PATH, its daemon answering, and the build image present;
   - the QEMU binary, and that it has the `esp32s3` machine;
   - each pinned image, by hash;
   - which probes are built.
   A missing image or probe is a warning, since a run needs only the image
   it boots; anything a run cannot start without is an error, and the exit
   status says so.

4. **The README's first section gives the three commands**: build the
   image, build QEMU, fetch the firmware. Then `tools/doctor.py`.

## Consequences

**Good**

- A new machine goes from clone to a booting device without reading a
  manifest by hand, and the slow tests stop skipping for want of a file.
- An image that changes upstream is caught by its hash, and one that moves
  upstream is still fetched from the commit it came from.

**Costs, accepted**

- Fetching downloads from GitHub and from Meshtastic's release, about
  30 MB. That is the user's to run; nothing does it implicitly.
- A commit URL serves the file only while GitHub keeps the commit. If a
  repository is rewritten, the hash still guards the content, and a new
  pin needs an ADR line.
