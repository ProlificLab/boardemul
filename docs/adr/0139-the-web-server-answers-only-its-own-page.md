# ADR 0139 — The web server answers only its own page

**Status:** Accepted — 2026-11-18. Amends ADR 0135, whose server took any
request that reached 127.0.0.1.

## Context

ADR 0135 bound the web front door to 127.0.0.1 and called it "a localhost
tool, not a service". Binding to loopback keeps other machines out. It does
not keep other web pages out, because the user's own browser is on loopback
too:

- **Any page can send to it.** A page on any site can `fetch` or submit a
  form to `http://127.0.0.1:8420/...`. With `mode: "no-cors"` and a
  `text/plain` body the browser sends the request without asking the server
  first; the page cannot read the answer, but the server acts on it. The
  server parsed every body as JSON whatever its `Content-Type`, and checked
  neither `Origin` nor `Host`.
- **A renamed host can read it.** DNS rebinding points an attacker's name at
  127.0.0.1 after the page has loaded, so the page's own origin now reaches
  the server and can read every answer. Only the `Host` header still carries
  the attacker's name.
- **What the API could be made to do.**
  - `POST /api/devices/<name>/start` passed its `args` straight onto the
    `emu/run.py` command line. `--qemu PATH` makes `run.py` execute PATH;
    `--outdir`, `--overlay` and `--air` name files and directories.
  - `POST /api/firmware` writes the request's bytes to
    `devices/.uploads/<seconds>-<name>`, a name a page can guess to the
    second.
  - Together: write a program, then start a device with it as its QEMU.
    Any site open in the browser while the server runs could run code on
    the machine. Recent Chrome asks before a public page reaches the local
    network; other browsers, and older Chromes, do not.

Nothing the page itself does needs any of this: it is served by the server,
from the server's own origin, and it sends JSON with an explicit content
type everywhere except the firmware upload. The one caller of `args` is the
Max WiFi slow test, which passes `--wifi-ap`.

## Decision

1. **Every request must name the server in `Host`**: `127.0.0.1`,
   `localhost` or `[::1]`, at the port it listens on, or a name the server
   was explicitly told it is reached by. Anything else is refused with 403,
   on every method, so a rebound name reads nothing.

2. **A request that changes something (POST, DELETE) and carries an
   `Origin` must carry the server's own**: `http://` plus one of the accepted
   hosts. Browsers send `Origin` on every cross-origin POST and DELETE,
   `no-cors` included, so a page from elsewhere is refused with 403. A
   request with no `Origin` is a program, not a page — the tests, `curl` —
   and is let through.

3. **Bodies must say what they are.** A JSON body needs
   `Content-Type: application/json`; the firmware upload needs
   `application/octet-stream` and is capped at 64 MiB. Neither is a type a
   page can send cross-origin without the browser asking the server first,
   and the server grants no such request. Anything else is refused with 415.
   This is the second lock on the door decision 2 closes.

4. **Run arguments are a list of per-run settings, not a command line.**
   `start` accepts only the radio and wiring flags that describe how a
   device is placed in the world: `--wifi-ap`, `--wifi-wpa2`, `--wifi-pass`,
   `--wifi-uplink`, `--gps`, `--at`, `--antenna`, `--environment`,
   `--obstruction`, `--icount` and `--reboot`, each with a value where it
   takes one, and no value starting with `--`. Anything that names a file, a
   directory or a program (`--qemu`, `--outdir`, `--builddir`, `--overlay`,
   `--air`, `--ble-air`, `--trace`, ...) is refused with 400: those belong to
   a person typing a command line, who can type it.

## Consequences

**Good**

- A page from anywhere else can neither drive nor read the server, in any
  browser, whatever its local-network policy.
- Even the page itself, or anything that gets the server to start a run,
  cannot make it execute a program or write outside the device folders.

**Costs, accepted**

- A setting that is not on the list cannot be passed from the browser until
  it is added, with a reason. Sharing an air between two devices (`--air`)
  is the first candidate, and is left out here because it names a directory.
- A server reached through a forwarded port under another name has to be
  told that name, or it refuses its own page.
