#!/usr/bin/env bash
# Where QEMU spends the host's time during a run (ADR 0176). In the build
# container, from /work:
#
#   bash tools/profile.sh [PERIOD_US] -- emu/run.py ARGS...
#
# Builds tools/qemu_sampler.c, runs the command with QEMU under the sampler
# (every PERIOD_US, default 1000, each QEMU thread is stopped for an instant
# and its place recorded), then reports per thread. The samples stay behind
# for tools/sample_report.py SAMPLES TOP --callers NAME. TOP=N sets how many
# functions a thread's report lists.
set -eu
period=1000
if [ "${1:-}" != "--" ] && [ $# -gt 0 ]; then
  period=$1
  shift
fi
[ "${1:-}" = "--" ] && shift
if [ $# -eq 0 ]; then
  echo "usage: bash tools/profile.sh [PERIOD_US] -- emu/run.py ARGS..." >&2
  exit 2
fi
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d /tmp/profile.XXXXXX)
gcc -O2 -Wall -o "$work/qemu_sampler" "$here/qemu_sampler.c"
qemu=${BOARDEMUL_QEMU:-$here/../third_party/qemu/build/qemu-system-xtensa}
# Each QEMU the run starts gets its own file: run.py also asks QEMU for its
# version, and that would overwrite the run's samples.
cat > "$work/qemu" <<EOF
#!/bin/sh
exec "$work/qemu_sampler" "$work/samples.\$\$.txt" $period -- "$qemu" "\$@"
EOF
chmod +x "$work/qemu"
python3 "$@" --qemu "$work/qemu" || echo "(the run exited with $?)"
samples=$(ls -S "$work"/samples.*.txt | head -1)
python3 "$here/sample_report.py" "$samples" "${TOP:-25}"
echo "samples: $samples"
echo "callers: python3 tools/sample_report.py $samples 25 --callers NAME"
