#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
#
# Download Espressif's official prebuilt QEMU for the same tag we build from
# source. This is never used to run the emulator (ADR 0002, decision 6).
#
# It exists to answer one question quickly: when our build misbehaves, run the
# identical flash image on this binary. Same result means the fork behaves that
# way and the problem is elsewhere. Different result means our build is at
# fault. That distinction otherwise costs an afternoon.
#
set -euo pipefail

QEMU_TAG="esp-develop-9.2.2-20260417"
ASSET="qemu-xtensa-softmmu-esp_develop_9.2.2_20260417-x86_64-linux-gnu.tar.xz"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${REPO_ROOT}/third_party/qemu-oracle"

mkdir -p "${DEST}"
echo "==> downloading ${ASSET}"
curl -fsSL -o "${DEST}/${ASSET}" \
    "https://github.com/espressif/qemu/releases/download/${QEMU_TAG}/${ASSET}"

echo "==> extracting"
tar -xf "${DEST}/${ASSET}" -C "${DEST}" --strip-components=1

echo
"${DEST}/bin/qemu-system-xtensa" --version | head -1
echo "oracle binary: ${DEST}/bin/qemu-system-xtensa"
