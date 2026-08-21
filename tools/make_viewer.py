#!/usr/bin/env python3
"""Build a single self-contained HTML viewer from a run's captured frames.

The frames are embedded as data URIs so the page works with no server and no
network, which is the only thing that makes it useful as a record of a run.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PAGE = """<title>T-Deck Max Panel</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+Condensed:wght@600;700&family=IBM+Plex+Sans:wght@400;500&display=swap">
<style>
:root {
  --paper:#E6E8E3; --surface:#F7F8F5; --ink:#15181A; --muted:#666C70;
  --line:#CDD1CA; --signal:#0E6E78; --amber:#9C6414; --screen:#D8DAD4;
}
:root:not([data-theme="light"]) { }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --paper:#121416; --surface:#1A1D20; --ink:#E7E9E4; --muted:#969CA0;
    --line:#2A2E32; --signal:#46B5BF; --amber:#D2963C; --screen:#0C0E10;
  }
}
:root[data-theme="dark"] {
  --paper:#121416; --surface:#1A1D20; --ink:#E7E9E4; --muted:#969CA0;
  --line:#2A2E32; --signal:#46B5BF; --amber:#D2963C; --screen:#0C0E10;
}
* { box-sizing:border-box; }
body {
  margin:0; background:var(--paper); color:var(--ink);
  font-family:"IBM Plex Sans",system-ui,sans-serif; line-height:1.55;
}
.wrap { max-width:60rem; margin:0 auto; padding:3rem 1.5rem 4rem; }
.eyebrow {
  font-family:"IBM Plex Mono",monospace; font-size:.72rem; letter-spacing:.14em;
  text-transform:uppercase; color:var(--signal); margin:0 0 .6rem;
}
h1 {
  font-family:"IBM Plex Sans Condensed",sans-serif; font-weight:700;
  font-size:clamp(2rem,5vw,3rem); line-height:1.05; margin:0 0 .75rem;
  text-wrap:balance; letter-spacing:-.01em;
}
.lede { max-width:34rem; color:var(--muted); margin:0 0 2.5rem; font-size:1.05rem; }
.stage { display:flex; flex-direction:column; align-items:center; gap:1rem; }
.bezel {
  background:var(--surface); border:1px solid var(--line); border-radius:6px;
  padding:1.25rem; box-shadow:0 1px 0 var(--line);
}
.screen {
  display:block; width:min(100%,40rem); aspect-ratio:4/3; background:var(--screen);
  image-rendering:pixelated; border:1px solid var(--line);
}
.controls { display:flex; align-items:center; gap:.75rem; flex-wrap:wrap; justify-content:center; }
button {
  font-family:"IBM Plex Mono",monospace; font-size:.8rem; letter-spacing:.04em;
  background:var(--surface); color:var(--ink); border:1px solid var(--line);
  border-radius:3px; padding:.45rem .9rem; cursor:pointer;
}
button:hover { border-color:var(--signal); color:var(--signal); }
button:focus-visible { outline:2px solid var(--signal); outline-offset:2px; }
.counter {
  font-family:"IBM Plex Mono",monospace; font-size:.8rem; color:var(--muted);
  font-variant-numeric:tabular-nums;
}
.scrub { display:flex; gap:.3rem; }
.tick {
  width:1.6rem; height:.35rem; border:0; padding:0; border-radius:2px;
  background:var(--line); cursor:pointer;
}
.tick[aria-current="true"] { background:var(--signal); }
h2 {
  font-family:"IBM Plex Sans Condensed",sans-serif; font-size:1.2rem;
  margin:3rem 0 1rem; padding-bottom:.5rem; border-bottom:1px solid var(--line);
}
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(11rem,1fr)); gap:1px;
        background:var(--line); border:1px solid var(--line); }
.cell { background:var(--surface); padding:1rem 1.1rem; }
.cell dt { font-family:"IBM Plex Mono",monospace; font-size:.7rem; letter-spacing:.1em;
           text-transform:uppercase; color:var(--muted); margin:0 0 .35rem; }
.cell dd { margin:0; font-family:"IBM Plex Sans Condensed",sans-serif; font-weight:700;
           font-size:1.5rem; font-variant-numeric:tabular-nums; }
.cell dd small { font-weight:400; font-size:.8rem; color:var(--muted); font-family:"IBM Plex Sans",sans-serif; }
.note { color:var(--muted); font-size:.92rem; max-width:38rem; }
code { font-family:"IBM Plex Mono",monospace; font-size:.85em; color:var(--amber); }
@media (prefers-reduced-motion:reduce) { .screen { transition:none; } }
</style>
<div class="wrap">
  <p class="eyebrow">BoardEmul &middot; LilyGO T-Deck Max</p>
  <h1>What the panel actually drew</h1>
  <p class="lede">
    Frames captured off the emulated SPI2 bus while LilyGO's own unmodified
    e-paper demo binary ran under QEMU. Nothing here is a mock-up: each image is
    the GDEQ031T10's frame buffer, decoded from the bytes the firmware sent.
  </p>

  <div class="stage">
    <div class="bezel"><img class="screen" id="screen" alt="Emulated e-paper panel"></div>
    <div class="controls">
      <button id="play">Pause</button>
      <button id="prev">&larr; Prev</button>
      <button id="next">Next &rarr;</button>
      <span class="counter" id="counter"></span>
      <span class="scrub" id="scrub"></span>
    </div>
  </div>

  <h2>The run that produced them</h2>
  <dl class="grid" id="stats"></dl>

  <h2>What had to work first</h2>
  <p class="note">
    The panel is driven over SPI2 with chip-select and data/command on ordinary
    GPIOs, so the frames only decode once GPIO levels are tracked too. Two
    self-clearing trigger bits &mdash; <code>I2C_CTR.TRANS_START</code> and
    <code>SPI_CMD.USR</code> &mdash; had to be masked out of stored register
    state; left in, the driver's own read-modify-write re-fires every transfer
    and the firmware appears to hang. The controller turned out to be a
    UC825x part using <code>0x10</code>/<code>0x13</code> for its data planes,
    not the SSD16xx command set its part number suggests.
  </p>
</div>
<script>
const FRAMES = __FRAMES__;
const STATS = __STATS__;
const img = document.getElementById('screen');
const counter = document.getElementById('counter');
const scrub = document.getElementById('scrub');
const playBtn = document.getElementById('play');
let i = 0, playing = true, timer = null;

document.getElementById('stats').innerHTML = STATS.map(s =>
  `<div class="cell"><dt>${s.label}</dt><dd>${s.value} <small>${s.unit||''}</small></dd></div>`
).join('');

FRAMES.forEach((_, n) => {
  const b = document.createElement('button');
  b.className = 'tick';
  b.setAttribute('aria-label', 'Frame ' + (n + 1));
  b.onclick = () => { stop(); show(n); };
  scrub.appendChild(b);
});

function show(n) {
  i = (n + FRAMES.length) % FRAMES.length;
  img.src = FRAMES[i];
  counter.textContent = String(i + 1).padStart(2, '0') + ' / ' + String(FRAMES.length).padStart(2, '0');
  [...scrub.children].forEach((t, k) => t.setAttribute('aria-current', k === i));
}
function stop() { playing = false; clearInterval(timer); playBtn.textContent = 'Play'; }
function start() {
  playing = true; playBtn.textContent = 'Pause';
  timer = setInterval(() => show(i + 1), 1400);
}
playBtn.onclick = () => playing ? stop() : start();
document.getElementById('next').onclick = () => { stop(); show(i + 1); };
document.getElementById('prev').onclick = () => { stop(); show(i - 1); };
show(0); start();
</script>
"""


def frame_sort_key(p: Path) -> tuple[int, str]:
    m = re.search(r"(\d+)", p.stem)
    return (int(m.group(1)) if m else 0, p.stem)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames", type=Path, default=REPO_ROOT / "logs" / "frames")
    ap.add_argument("--manifest", type=Path, default=None,
                    help="run manifest json, for the statistics panel")
    ap.add_argument("-o", "--out", type=Path, default=REPO_ROOT / "logs" / "panel.html")
    args = ap.parse_args(argv)

    paths = [p for p in sorted(args.frames.glob("frame-*.png"), key=frame_sort_key)]
    if not paths:
        print(f"no frames in {args.frames}")
        return 1

    encoded = ["data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()
               for p in paths]

    stats = [{"label": "Frames drawn", "value": len(paths), "unit": ""}]
    if args.manifest and args.manifest.exists():
        m = json.loads(args.manifest.read_text())
        stats += [
            {"label": "Run outcome", "value": m.get("exit_reason", "?"), "unit": ""},
            {"label": "Wall time", "value": m.get("wall_seconds", "?"), "unit": "s"},
            {"label": "QEMU", "value": m.get("qemu_version", "?").split()[-1], "unit": ""},
        ]

    html = (PAGE.replace("__FRAMES__", json.dumps(encoded))
                .replace("__STATS__", json.dumps(stats)))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html, encoding="utf-8")
    print(f"wrote {args.out}  ({len(paths)} frames, {args.out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
