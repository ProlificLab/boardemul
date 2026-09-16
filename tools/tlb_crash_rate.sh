#!/usr/bin/env bash
#
# ADR 0041's reproduction, as a number.
#
# The crash it describes -- QEMU dying with a segmentation fault under two
# translation threads -- is rare enough that "it did not happen" means nothing
# on its own, which is how it survived weeks as an open note. This runs rounds
# of N concurrent Meshtastic boots, each with its own build directory, and
# counts the ones that ended on a signal. ADR 0066 fixes the defect and quotes
# the before and after from here.
#
#   tools/tlb_crash_rate.sh LABEL [ROUNDS] [CONCURRENCY]
#
# Two things to know before reading a result:
#
#  * The rate is low -- about one boot in a hundred on this machine -- so a
#    zero over a few dozen runs is not evidence. To make a difference visible
#    in minutes rather than hours, build with the amplifier first:
#
#      BOARDEMUL_QEMU_EXPERIMENTS=amplify-cross-thread-tlb-flush tools/build_qemu.sh
#
#    which widens the window and raises the rate by more than an order of
#    magnitude. See qemu-patch/experiments/README.md.
#
#  * More concurrency is not more load in the sense that matters. At twice the
#    core count each guest runs slower, the window shrinks against its
#    progress, and the rate goes *down*: measured 1 in 56 at fourteen and 1 in
#    168 at twenty-eight. Fourteen, the core count here, is what ADR 0041 used.
#
# Run artefacts go under /tmp rather than the working tree on purpose: writing
# them across a bind mount changes guest timing (ADR 0063).
#
# A fault shows up here as a raw signal -- "guest exited (code -11)" -- not as
# the address ADR 0041 captured. That report comes from the bus bridge's own
# handler, which is only installed when a board is built, and this deliberately
# runs without --bus so that the workload is the guest and nothing else. Add
# --bus below if you want the faulting address rather than only the count.
set -u
LABEL=${1:-run}
ROUNDS=${2:-4}
WIDE=${3:-14}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FW="${REPO_ROOT}/firmware/firmware-t-deck-tft-2.7.10.94d4bdf.bin"
LFS="${REPO_ROOT}/firmware/littlefs-t-deck-tft-2.7.10.94d4bdf.bin"
OUT="/tmp/tlb-rate-${LABEL}"

if [[ ! -f "${FW}" || ! -f "${LFS}" ]]; then
    echo "ERROR: the Meshtastic firmware and its filesystem image are needed." >&2
    echo "       See firmware/MANIFEST.toml for how to fetch them." >&2
    exit 1
fi

rm -rf "${OUT}"; mkdir -p "${OUT}"
total=0
faults=0

for r in $(seq 1 "${ROUNDS}"); do
    pids=""
    for i in $(seq 1 "${WIDE}"); do
        BOARDEMUL_TCG_THREADS=multi BOARDEMUL_CATCH_SEGV=1 \
        python3 "${REPO_ROOT}/emu/run.py" "${FW}" \
            --board tdeck --psram-octal \
            --overlay "0xc90000=${LFS}" \
            --outdir "${OUT}/r${r}_$i" --builddir "${OUT}/b${r}_$i" \
            --timeout 60 > "${OUT}/r${r}_$i.out" 2>&1 &
        pids="${pids} $!"
    done
    wait ${pids}

    for i in $(seq 1 "${WIDE}"); do
        total=$((total + 1))
        # A signal is the one ending no guest can cause: 139 is the bus
        # bridge's own fault handler exiting 128 + SIGSEGV, -11 an uncaught
        # one. A stall or a timeout is a different complaint and not counted.
        if grep -qE "result  : guest exited \(code (139|-11|138|-10|134|-6)\)" \
                 "${OUT}/r${r}_$i.out" 2>/dev/null; then
            faults=$((faults + 1))
            echo "--- FAULT round $r run $i"
            grep -h "result  :" "${OUT}/r${r}_$i.out" | head -1
            grep -h "signal" "${OUT}/r${r}_$i"/*.stderr.log 2>/dev/null | head -4
        fi
    done
    echo "[${LABEL}] after round $r: ${faults} fault(s) in ${total} run(s)"
    rm -rf "${OUT}"/b*    # 8 MB of flash image each; the logs are the evidence
done

echo "[${LABEL}] RESULT: ${faults} fault(s) in ${total} run(s)"
