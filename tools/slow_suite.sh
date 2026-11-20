#!/usr/bin/env bash
# Run the slow tier N files at a time, then the files marked `alone` one at a
# time with nothing else booting (ADR 0167). In the build container, from the
# repository's root:
#
#   bash tools/slow_suite.sh [N]       # N defaults to 3
#
# Each shard is its own pytest with its own basetemp under /tmp, its output in
# logs/slow-shard-K.txt and the alone files' in logs/slow-alone.txt. The exit
# status is non-zero if any part failed. `pytest -m slow` is the plain way,
# one test at a time, and needs none of this.
set -u
SHARDS=${1:-3}
cd "$(dirname "$0")/.."
mkdir -p logs
mapfile -t SLOW < <(grep -l "mark.slow" tests/test_*.py | sort)
mapfile -t ALONE < <(grep -l "mark.alone" "${SLOW[@]}" | sort)
SHARED=()
for f in "${SLOW[@]}"; do
    grep -q "mark.alone" "$f" || SHARED+=("$f")
done
echo "slow tier: ${#SHARED[@]} file(s) ${SHARDS} at a time, then ${#ALONE[@]} alone"
rm -rf /tmp/slow-shard-* /tmp/slow-alone
status=0
pids=()
for s in $(seq 0 $((SHARDS - 1))); do
    mine=()
    for i in "${!SHARED[@]}"; do
        if [ $((i % SHARDS)) -eq "$s" ]; then mine+=("${SHARED[$i]}"); fi
    done
    [ ${#mine[@]} -gt 0 ] || continue
    python3 -m pytest "${mine[@]}" -m 'slow or not slow' -p no:cacheprovider \
        --basetemp=/tmp/slow-shard-$s -q -rfE > logs/slow-shard-$s.txt 2>&1 &
    pids+=($!)
done
for pid in "${pids[@]}"; do
    wait "$pid" || status=1
done
if [ ${#ALONE[@]} -gt 0 ]; then
    python3 -m pytest "${ALONE[@]}" -m 'slow or not slow' -p no:cacheprovider \
        --basetemp=/tmp/slow-alone -q -rfE > logs/slow-alone.txt 2>&1 || status=1
fi
for f in logs/slow-shard-*.txt logs/slow-alone.txt; do
    [ -f "$f" ] || continue
    echo "== $f"
    grep -E "passed|failed|error|FAILED|ERROR" "$f" | tail -12
done
exit $status
