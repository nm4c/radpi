#!/bin/bash
# Set up the RadPi HAT codec's (TLV320AIC3104) inputs and outputs, then save them so they come back
# at every boot (alsactl store; restored by alsa-restore.service). Needs the radpi-codec overlay
# (deploy/overlays/radpi-codec.dts), so that `aplay -l` lists card "RadPi".
#
#   sudo deploy/radpi-mixer.sh sa818      RadPi SA818 HAT: TX on HPLOUT only (single-ended)
#   sudo deploy/radpi-mixer.sh control    RadPi Control HAT: TX on HPLOUT + HPLCOM (differential, into T1)
#   radpi-mixer --boot                    run at every boot by radpi-mixer.service (deploy/setup-hat.sh
#                                         sets it up): waits for the card, then puts back the levels
#                                         saved with alsactl store, or sets it up as above the first time
#                                         (the HAT from /etc/radpi-hat). The system's own restore can run
#                                         before the card appears, leaving the driver's defaults (PCM at
#                                         -23.5 dB, the routing off).
#
# Receive audio comes in on LINE1L (pins 10/11, RX_P/RX_N) on both HATs.
# Levels after this: everything at 0 dB. Set the on-air levels with audio.rx_gain / audio.tx_gain in
# config.yaml, or adjust here: 'PGA' (capture, 0 to 59.5 dB) and 'HP' (output, 0 to 9 dB).
set -euo pipefail

firstboot=0
if [[ "${1:-}" == --boot || "${1:-}" == --firstboot ]]; then
  firstboot=1
  set -- "$(cat /etc/radpi-hat 2>/dev/null || true)"
  # the codec's driver can still be loading this early in boot
  for _ in $(seq 30); do aplay -l 2>/dev/null | grep -q "\[RadPi\]" && break; sleep 1; done
  if [[ -e /var/lib/radpi/mixer-done ]] && alsactl restore RadPi 2>/dev/null; then
    echo "Codec levels restored (saved with: sudo alsactl store RadPi)."
    exit 0
  fi
fi
hat="${1:-}"
if [[ "$hat" != "sa818" && "$hat" != "control" ]]; then
  echo "usage: sudo $0 sa818|control"; exit 1
fi
card=RadPi
failed=0
m() { amixer -q -c "$card" sset "$@" 2>/dev/null || { echo "  couldn't set: $*"; failed=1; }; }

aplay -l | grep -q "\[$card\]" || { echo "No sound card $card: is the radpi-codec overlay loaded?"; exit 1; }

# ---- receive: LINE1L (differential pair) -> left and right PGA -> ADC
for side in Left Right; do
  m "$side Line1L Mux" differential
  m "$side PGA Mixer Line1L" on
  m "$side Line1R Mux" differential
  m "$side PGA Mixer Line1R" off
  m "$side PGA Mixer Mic2L" off
  m "$side PGA Mixer Mic2R" off
done
m PGA 0dB cap
m AGC off
# the ADC's own DC filter: without it an input offset turns every switch (PTT) into a pop
m "ADC HPF Cut-off" 0.0045xFs

# ---- transmit: DAC left -> HPLOUT (and HPLCOM on the Control HAT)
m "Left DAC Mux" DAC_L1
m "Right DAC Mux" DAC_R1
m PCM 0dB unmute
for mixer in "Left HP Mixer" "Right HP Mixer" "Left HPCOM Mixer" "Right HPCOM Mixer" \
             "Left Line Mixer" "Right Line Mixer"; do
  for src in DACL1 DACR1 "PGAL Bypass" "PGAR Bypass"; do
    m "$mixer $src" off
  done
done
m "Left HP Mixer DACL1" on
m "HP DAC" 0dB
m HP 0dB unmute
if [[ "$hat" == control ]]; then
  m "Left HPCOM Mux" "differential of HPLOUT"
  m "HPCOM DAC" 0dB
  m HPCOM 0dB unmute
else
  m HPCOM mute
fi
m Line mute

alsactl store "$card"
echo "Codec set up for the $hat HAT and saved."
if (( firstboot )); then
  mkdir -p /var/lib/radpi && touch /var/lib/radpi/mixer-done
fi
[[ $failed == 0 ]] || echo "Some settings didn't apply (above): compare with 'amixer -c RadPi scontrols'."
