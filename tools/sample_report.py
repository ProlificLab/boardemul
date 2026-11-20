#!/usr/bin/env python3
"""Where the sampled threads were (ADR 0176): per thread, the share of
samples in each function of QEMU or its libraries, or in TCG's generated
code; or, with --callers NAME, the QEMU functions found above NAME on the
stack. Those are scanned stack words, not an unwinder: a good guess.

    sample_report.py SAMPLES [TOP] [--callers NAME]

SAMPLES is what tools/qemu_sampler.c wrote; tools/profile.sh runs both.
Libraries without a symbol table are named by their nearest exported
symbol, which can be the wrong one.
"""
import bisect
import collections
import subprocess
import sys

samples_path = sys.argv[1]
top = int(sys.argv[2]) if len(sys.argv) > 2 else 25

maps = []
for line in open(samples_path + ".maps"):
    f = line.split()
    lo, hi = (int(x, 16) for x in f[0].split("-"))
    maps.append((lo, hi, f[1], int(f[2], 16), f[5] if len(f) > 5 else ""))
maps.sort()
starts = [m[0] for m in maps]
base = {}
for lo, hi, perms, off, path in maps:
    if path and off == 0 and path not in base:
        base[path] = lo

symbols = {}


def syms(path):
    if path not in symbols:
        table = []
        for flags in (["-n", "-C", "--defined-only"], ["-n", "-C", "-D", "--defined-only"]):
            try:
                out = subprocess.run(["nm", *flags, path], capture_output=True, text=True).stdout
            except OSError:
                out = ""
            for line in out.splitlines():
                parts = line.split(" ", 2)
                if len(parts) == 3 and parts[1] in "tTwW":
                    table.append((int(parts[0], 16), parts[2]))
            if table:
                break
        table.sort()
        symbols[path] = ([a for a, _ in table], [n for _, n in table])
    return symbols[path]


def where(rip):
    i = bisect.bisect_right(starts, rip) - 1
    if i < 0 or rip >= maps[i][1]:
        return "[unmapped]"
    lo, hi, perms, off, path = maps[i]
    if not path or path.startswith("["):
        return "[generated code]" if "x" in perms else f"[anon {perms}]"
    addrs, names = syms(path)
    rel = rip - base.get(path, lo)
    j = bisect.bisect_right(addrs, rel) - 1
    lib = path.rsplit("/", 1)[-1]
    if j < 0:
        return f"{lib}:?"
    name = names[j]
    if len(name) > 70:
        name = name[:67] + "..."
    return name if lib.startswith("qemu-system") else f"{name} ({lib})"


def in_qemu(rip):
    i = bisect.bisect_right(starts, rip) - 1
    return i >= 0 and rip < maps[i][1] and maps[i][4].rsplit("/", 1)[-1].startswith("qemu-system")


# --callers PATTERN: for samples in a function matching PATTERN, the QEMU
# functions found above it on the stack (scanned words, so a guess).
callers_of = None
if "--callers" in sys.argv:
    callers_of = sys.argv[sys.argv.index("--callers") + 1]
callers = collections.Counter()
chains = collections.Counter()

per_thread = collections.defaultdict(collections.Counter)
for line in open(samples_path):
    fields = line.split()
    tid, rip = fields[0], int(fields[1], 16)
    name = where(rip)
    per_thread[tid][name] += 1
    if callers_of and callers_of in name:
        frames = [where(int(a, 16)) for a in fields[2:] if in_qemu(int(a, 16))]
        if frames:
            callers[frames[0]] += 1
            chains[" < ".join(dict.fromkeys(frames[:3]))] += 1

if callers_of:
    total = sum(callers.values())
    print(f"=== first QEMU function above '{callers_of}': {total} samples")
    for name, n in callers.most_common(top):
        print(f"  {100 * n / total:5.1f}%  {name}")
    print("=== the first three")
    for name, n in chains.most_common(top):
        print(f"  {100 * n / total:5.1f}%  {name}")
    sys.exit(0)

for tid, counter in sorted(per_thread.items(), key=lambda kv: -sum(kv[1].values())):
    total = sum(counter.values())
    print(f"=== thread {tid}: {total} samples")
    for name, n in counter.most_common(top):
        print(f"  {100 * n / total:5.1f}%  {name}")
