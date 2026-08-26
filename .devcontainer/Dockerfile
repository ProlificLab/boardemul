FROM mcr.microsoft.com/devcontainers/base:ubuntu-24.04

# Build dependencies only. QEMU itself is built by tools/build_qemu.sh so that
# the compile is a step you can read and time, not an opaque image layer
# (ADR 0002, decision 2).
#
# meson is deliberately absent: Ubuntu 24.04 ships 1.3.2 and QEMU 9.2 needs
# 1.5.0+. QEMU's configure bootstraps a new enough meson into a private venv
# from wheels it carries, which is why python3-venv is here instead.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        libglib2.0-dev \
        libgcrypt20-dev \
        libpixman-1-dev \
        libslirp-dev \
        ninja-build \
        pkg-config \
        python3 \
        python3-pip \
        python3-pytest \
        python3-venv \
        xz-utils \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
