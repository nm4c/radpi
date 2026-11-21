#!/usr/bin/env bash
# Install the AI agent's packages (requirements-bot.txt) into RadPi's venv. Called by install.sh
# --with-ai and update.sh; usage: deploy/pip-ai.sh /opt/radpi
#
# Two things that otherwise go wrong on a Pi:
#  - PyTorch from PyPI on ARM now comes with gigabytes of NVIDIA CUDA libraries a Pi can't use, so
#    torch and torchaudio come from PyTorch's CPU-only index first (the rest then finds them there).
#  - /tmp is a RAM disk on current Raspberry Pi OS, too small for these downloads ("No space left on
#    device" with the SD card nearly empty), so pip works in /var/tmp, without keeping a cache.
set -euo pipefail
DEST="${1:?usage: $0 /opt/radpi}"
PIP="$DEST/.venv/bin/pip"
work=/var/tmp/radpi-pip
mkdir -p "$work"
export TMPDIR="$work"
trap 'rm -rf "$work"' EXIT

# A GPU build left from an earlier install: swap it for the CPU one (and drop the CUDA libraries).
if "$PIP" list 2>/dev/null | grep -qi '^nvidia-'; then
  echo "   removing NVIDIA CUDA libraries a Pi can't use"
  "$PIP" list 2>/dev/null | awk 'tolower($1) ~ /^nvidia-|^triton$/ {print $1}' | xargs -r "$PIP" uninstall -y -q
  "$PIP" uninstall -y -q torch torchaudio || true
fi
"$PIP" install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch torchaudio
"$PIP" install --no-cache-dir -r "$DEST/requirements-bot.txt"
