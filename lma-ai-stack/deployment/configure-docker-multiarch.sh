#!/usr/bin/env bash
# Copyright (c) 2026 Amazon.com
# Licensed under the MIT License. See LICENSE in the project root.
set -euo pipefail

# SAM builds x86_64 packages. Native Linux runners need no privileged emulator
# installation or Docker Hub pull; Docker Desktop supplies emulation on macOS.
case "$(uname -s):$(uname -m)" in
  Linux:x86_64|Linux:amd64)
    echo '[INFO] Native x86_64 build; QEMU setup is unnecessary'
    ;;
  Linux:*)
    docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
    ;;
  *)
    echo '[INFO] Skipping QEMU setup; Docker host provides container emulation'
    ;;
esac
