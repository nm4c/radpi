#!/usr/bin/env bash
# Set a Raspberry Pi up for a RadPi HAT, or check that it is. Run from the git clone:
#
#   sudo ./deploy/setup-hat.sh control    RadPi Control HAT (external radio)
#   sudo ./deploy/setup-hat.sh sa818      RadPi SA818 HAT (built-in transceiver)
#   sudo ./deploy/setup-hat.sh --check    what's set up, what isn't, and how to fix it
#
# Works out whether it's a Pi 4 or a Pi 5 and does what each needs:
#   - the codec overlay (deploy/overlays/radpi-codec.dts) built and installed, with I2C and I2S on,
#     so the HAT's codec is sound card "RadPi"
#   - the codec's inputs and outputs (deploy/radpi-mixer.sh), set once at the next boot by
#     radpi-mixer.service, when the card first exists
#   - SA818 HAT only: the UART on GPIO14/15 for the module. Pi 4: Bluetooth moved off it
#     (disable-bt) and enable_uart=1. Pi 5: uart0 on (it's /dev/ttyAMA0 there; /dev/serial0 is the
#     debug connector). Both: no serial console or login on it, which would eat the module's replies
# Changes to config.txt go in one marked block (rewritten on each run, so it's safe to run again
# or to switch HATs); the first run keeps a copy of config.txt and cmdline.txt as *.radpi-bak.
# Then reboot. config.yaml needs radio.type (buttons or sa818) and the channels; the server finds
# the sound card and the SA818's port by itself.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/radpi
BEGIN="# --- RadPi HAT (deploy/setup-hat.sh): edits here are replaced on the next run ---"
END="# --- end RadPi HAT ---"

usage() { sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
case "${1:-}" in
  control|sa818) hat=$1; mode=setup ;;
  --check) hat=""; mode=check ;;
  *) usage ;;
esac
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

BOOT=/boot/firmware; [[ -d $BOOT ]] || BOOT=/boot
CONFIG_TXT=$BOOT/config.txt
CMDLINE=$BOOT/cmdline.txt
model="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo "not a Raspberry Pi")"
case "$model" in
  *"Pi 5"*|*"Pi 500"*|*"Compute Module 5"*) gen=5 ;;
  *"Pi 4"*|*"Pi 400"*|*"Compute Module 4"*) gen=4 ;;
  *"Raspberry Pi"*) gen=3 ;;
  *) gen=0 ;;
esac
if (( gen == 5 )); then sa818_port=/dev/ttyAMA0; else sa818_port=/dev/serial0; fi

# ============================================================================== check
if [[ $mode == check ]]; then
  problems=0
  ok()  { printf '  ok    %s\n' "$*"; }
  bad() { printf '  FIX   %s\n' "$1"; [[ -n "${2:-}" ]] && printf '        -> %s\n' "$2"; problems=$((problems + 1)); }
  note(){ printf '  --    %s\n' "$*"; }

  echo "== $model"
  hat="$(cat /etc/radpi-hat 2>/dev/null || true)"
  [[ -n $hat ]] && ok "set up for the $hat HAT (/etc/radpi-hat)" \
    || bad "setup-hat.sh hasn't been run" "sudo ./deploy/setup-hat.sh control   (or sa818), then reboot"

  echo "== sound card (codec)"
  [[ -f $BOOT/overlays/radpi-codec.dtbo ]] && ok "overlay installed" \
    || bad "no $BOOT/overlays/radpi-codec.dtbo" "sudo ./deploy/setup-hat.sh ${hat:-control}"
  grep -qE '^\s*dtoverlay=radpi-codec' "$CONFIG_TXT" && ok "config.txt loads it" \
    || bad "config.txt doesn't load the overlay" "sudo ./deploy/setup-hat.sh ${hat:-control}, then reboot"
  if aplay -l 2>/dev/null | grep -q '\[RadPi\]'; then
    ok "sound card RadPi is there"
    db() { amixer -c RadPi sget "$1" 2>/dev/null | grep -oE '\[-?[0-9.]+dB\]' | head -1 | tr -d '[]dB' || true; }
    note "levels: PCM $(db PCM) dB, HP $(db HP) dB (transmit); PGA $(db PGA) dB (receive)"
    pcm="$(db PCM)"
    if [[ -n $pcm ]] && awk "BEGIN{exit !($pcm < -1)}"; then
      bad "PCM (transmit level) is at $pcm dB: it should be 0" "sudo /usr/local/sbin/radpi-mixer ${hat:-sa818}"
    fi
    [[ -e /var/lib/radpi/mixer-done ]] || note "codec routing not set at boot yet: sudo /usr/local/sbin/radpi-mixer ${hat:-sa818}"
  elif [[ -f $BOOT/overlays/radpi-codec.dtbo ]] && grep -qE '^\s*dtoverlay=radpi-codec' "$CONFIG_TXT"; then
    bad "no sound card RadPi, although the overlay is set up" \
        "reboot if you haven't since setting up; otherwise: dmesg | grep -iE 'aic3x|i2s'  (is the HAT seated?)"
  else
    bad "no sound card RadPi" "sudo ./deploy/setup-hat.sh ${hat:-control}, then reboot"
  fi

  radio_type=""
  if [[ -x $DEST/.venv/bin/python && -f $DEST/config.yaml ]]; then
    radio_type="$(cd "$DEST" && "$DEST/.venv/bin/python" - 2>/dev/null <<'PY' || true
from server import config
c = config.load("config.yaml")
print(c["radio"]["type"])
PY
)"
  fi

  if [[ $hat == sa818 || $radio_type == sa818 ]]; then
    echo "== SA818 serial port ($sa818_port on this Pi)"
    [[ -e $sa818_port ]] && ok "$sa818_port exists" \
      || bad "$sa818_port is missing" "sudo ./deploy/setup-hat.sh sa818, then reboot"
    if (( gen == 5 )); then
      grep -qE '^\s*dtparam=uart0=on' "$CONFIG_TXT" && ok "uart0 on" \
        || bad "uart0 isn't on" "sudo ./deploy/setup-hat.sh sa818, then reboot"
    elif (( gen > 0 )); then
      target="$(readlink /dev/serial0 2>/dev/null || true)"
      [[ $target == ttyAMA0 ]] && ok "serial0 -> ttyAMA0 (Bluetooth moved off it)" \
        || bad "serial0 -> ${target:-nothing}: the mini UART or Bluetooth has the pins" "sudo ./deploy/setup-hat.sh sa818, then reboot"
    fi
    if grep -qE 'console=(serial0|ttyAMA0|ttyS0)' "$CMDLINE"; then
      bad "cmdline.txt puts a console on the port (it eats the module's replies)" "sudo ./deploy/setup-hat.sh sa818, then reboot"
    else
      ok "no serial console on the port"
    fi
    getty=""
    for t in ttyAMA0 ttyS0 serial0; do
      systemctl is-active -q "serial-getty@$t.service" 2>/dev/null && getty+="$t "
    done
    [[ -z $getty ]] && ok "no login prompt on the port" \
      || bad "a login prompt is running on ${getty% }" "sudo ./deploy/setup-hat.sh sa818"
    id -nG radio 2>/dev/null | grep -qw dialout && ok "service user can use the port (dialout)" \
      || bad "user radio isn't in dialout" "sudo usermod -aG dialout radio"
  fi

  echo "== RadPi"
  if [[ -n $hat && -n $radio_type ]]; then
    want=$([[ $hat == sa818 ]] && echo sa818 || echo buttons)
    [[ $radio_type == "$want" ]] && ok "config.yaml: radio.type $radio_type" \
      || bad "config.yaml: radio.type is $radio_type, but this is the $hat HAT" "set radio.type: $want in $DEST/config.yaml"
  elif [[ ! -f $DEST/config.yaml ]]; then
    bad "RadPi isn't installed in $DEST" "sudo ./deploy/install.sh"
  fi
  if systemctl is-active -q radpi; then
    ok "radpi service running"
  else
    bad "radpi service isn't running" "sudo systemctl restart radpi; journalctl -u radpi -n 30 --no-pager"
  fi
  errors="$(journalctl -u radpi -b --no-pager -o cat 2>/dev/null | grep -E ' ERROR |Error|SystemExit' | tail -4 || true)"
  [[ -n $errors ]] && { note "recent errors in the log:"; sed 's/^/          /' <<<"$errors"; }

  echo
  (( problems == 0 )) && echo "All good." || echo "$problems thing(s) to fix (above)."
  exit 0
fi

# ============================================================================== setup
(( gen > 0 )) || { echo "This isn't a Raspberry Pi ($model)."; exit 1; }
echo "== $model: setting up for the RadPi ${hat} HAT"
reboot_needed=0

command -v dtc >/dev/null && command -v amixer >/dev/null || apt-get install -y device-tree-compiler alsa-utils

# ---- codec overlay
tmp="$(mktemp)"
dtc -q -@ -I dts -O dtb -o "$tmp" "$SRC/deploy/overlays/radpi-codec.dts"
if ! cmp -s "$tmp" "$BOOT/overlays/radpi-codec.dtbo"; then
  install -m 644 "$tmp" "$BOOT/overlays/radpi-codec.dtbo"
  echo "   codec overlay installed"
  reboot_needed=1
fi
rm -f "$tmp"

# ---- config.txt: one block of ours, at the end, for all models
lines=("dtparam=i2c_arm=on" "dtparam=i2s=on" "dtoverlay=radpi-codec")
if [[ $hat == sa818 ]]; then
  if (( gen == 5 )); then lines+=("dtparam=uart0=on"); else lines+=("enable_uart=1" "dtoverlay=disable-bt"); fi
fi
[[ -f $CONFIG_TXT.radpi-bak ]] || cp "$CONFIG_TXT" "$CONFIG_TXT.radpi-bak"
new="$(mktemp)"
# drop our old block, and loose copies of the lines we're about to add (set up by hand earlier)
awk -v b="$BEGIN" -v e="$END" -v keep="$(IFS='|'; echo "${lines[*]}")" '
  BEGIN { n = split(keep, k, "|"); for (i = 1; i <= n; i++) if (k[i] != "") ours[k[i]] = 1 }
  $0 == b { skip = 1; next }
  $0 == e { skip = 0; next }
  skip { next }
  { line = $0; gsub(/^[ \t]+|[ \t]+$/, "", line); if (line in ours) next; print }
' "$CONFIG_TXT" > "$new"
# no blank lines piling up at the end
sed -i -e :a -e '/^\n*$/{$d;N;ba' -e '}' "$new"
{ echo; echo "$BEGIN"; echo "[all]"; printf '%s\n' "${lines[@]}"; echo "$END"; } >> "$new"
if ! cmp -s "$new" "$CONFIG_TXT"; then
  cat "$new" > "$CONFIG_TXT"
  echo "   config.txt: ${lines[*]}"
  reboot_needed=1
fi
rm -f "$new"

# ---- SA818: nothing else on the UART
if [[ $hat == sa818 ]]; then
  [[ -f $CMDLINE.radpi-bak ]] || cp "$CMDLINE" "$CMDLINE.radpi-bak"
  newcmd="$(awk '{ out = ""; for (i = 1; i <= NF; i++) {
      if ($i ~ /^console=(serial0|ttyAMA0|ttyS0)(,[0-9]+)?$/) continue
      out = out (out == "" ? "" : " ") $i }
    print out }' "$CMDLINE")"
  if [[ "$newcmd" != "$(cat "$CMDLINE")" ]]; then
    printf '%s\n' "$newcmd" > "$CMDLINE"
    echo "   cmdline.txt: serial console removed"
    reboot_needed=1
  fi
  for t in ttyAMA0 ttyS0 serial0; do
    if systemctl is-enabled -q "serial-getty@$t.service" 2>/dev/null || systemctl is-active -q "serial-getty@$t.service" 2>/dev/null; then
      systemctl disable --now "serial-getty@$t.service" >/dev/null 2>&1 || true
      echo "   login prompt on $t stopped"
    fi
  done
  if (( gen < 5 )) && systemctl is-enabled -q hciuart 2>/dev/null; then
    systemctl disable --now hciuart >/dev/null 2>&1 || true
    echo "   hciuart (Bluetooth on the UART) disabled"
  fi
  id radio >/dev/null 2>&1 && usermod -aG dialout radio
fi

# ---- codec routing: now if the card is already there, else at the next boot. Only when the HAT
# is new or changed: running this again (update.sh does) mustn't reset levels set since.
old_hat="$(cat /etc/radpi-hat 2>/dev/null || true)"
[[ $old_hat == "$hat" ]] || rm -f /var/lib/radpi/mixer-done
echo "$hat" > /etc/radpi-hat
install -m 755 "$SRC/deploy/radpi-mixer.sh" /usr/local/sbin/radpi-mixer
install -m 644 "$SRC/deploy/radpi-mixer.service" /etc/systemd/system/radpi-mixer.service
systemctl daemon-reload
systemctl enable -q radpi-mixer.service
if [[ -e /var/lib/radpi/mixer-done ]]; then
  :   # already set up for this HAT
elif (( ! reboot_needed )) && aplay -l 2>/dev/null | grep -q '\[RadPi\]'; then
  /usr/local/sbin/radpi-mixer "$hat" && { mkdir -p /var/lib/radpi; touch /var/lib/radpi/mixer-done; }
else
  echo "   codec inputs and outputs: set at the next boot"
fi

echo
echo "Next, in $DEST/config.yaml:"
if [[ $hat == sa818 ]]; then
  echo "   radio.type: sa818, and channel.list (frequencies). The serial port ($sa818_port on this Pi),"
  echo "   the sound card and the busy pin (GPIO23) are found by themselves."
else
  echo "   radio.type: buttons, channel.count / names, and gpio.busy_pin: 23 if the radio's busy line"
  echo "   is wired to BS+/BS-. The sound card is found by itself."
fi
if (( reboot_needed )); then
  echo
  echo "Reboot now (sudo reboot), then check with:  sudo ./deploy/setup-hat.sh --check"
else
  echo "Nothing needed a reboot. Check with:  sudo ./deploy/setup-hat.sh --check"
fi
