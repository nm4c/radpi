#!/usr/bin/env bash
# Install (or update) RadPi on an ordinary Linux PC or server, for an Icom radio with an IP remote
# mic (radio.type icom): no HAT, GPIO or sound card needed. Works on Debian, Ubuntu, Fedora and
# AlmaLinux / Rocky / RHEL, on x86_64 or any other machine that isn't a Raspberry Pi.
#
#   git clone https://github.com/nm4c/radpi ~/radpi && cd ~/radpi
#   sudo ./deploy/install-x86.sh          (run again, or sudo ./deploy/update.sh, to update)
#   sudo ./deploy/install-x86.sh --with-ai   also the AI agent's packages (CPU PyTorch, Whisper,
#                                            Piper: a few GB); kept up to date on later runs
#
# It installs Python 3.10 or newer (AlmaLinux 9's own is too old: it adds python3.12), makes the
# 'radio' service user, puts the program in /opt/radpi with its own Python environment, starts
# /opt/radpi/config.yaml from config.icom.yaml (HTTPS on port 443), makes a self-signed certificate
# in /opt/radpi/tls if there's none yet (deploy/cert-cloudflare.sh replaces it with a trusted one),
# installs the service allowed to use port 443, and opens TCP 52001 and UDP 50000 (the radio) and
# the web port in firewalld or ufw if either is running.
set -euo pipefail

DEST=/opt/radpi
SRC="$(cd "$(dirname "$0")/.." && pwd)"
UNIT=/etc/systemd/system/radpi.service

with_ai=0
for arg in "$@"; do
  case "$arg" in
    --with-ai) with_ai=1 ;;
    *) echo "unknown option: $arg  (--with-ai)"; exit 1 ;;
  esac
done
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
[[ -r /etc/os-release ]] || { echo "can't tell which Linux this is (no /etc/os-release)"; exit 1; }
. /etc/os-release
case " ${ID:-} ${ID_LIKE:-} " in
  *" debian "*|*" ubuntu "*) family=apt ;;
  *" fedora "*|*" rhel "*|*" centos "*|*" almalinux "*|*" rocky "*) family=dnf ;;
  *) echo "Not a Linux this installer knows (${PRETTY_NAME:-$ID}): Debian, Ubuntu, Fedora or AlmaLinux"; exit 1 ;;
esac
update=0
[[ -x $DEST/.venv/bin/python ]] && update=1
echo "== ${PRETTY_NAME:-$ID}: $( ((update)) && echo updating || echo installing ) RadPi in $DEST"

# ---- Python 3.10 or newer, with venv
best_python() {
  for p in python3.13 python3.12 python3.11 python3.10 python3; do
    command -v "$p" >/dev/null && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null \
      && { command -v "$p"; return; }
  done
  return 1
}
if [[ $family == apt ]]; then
  apt-get update -qq
  apt-get install -y python3 python3-venv rsync ca-certificates openssl
else
  dnf install -y python3 rsync ca-certificates openssl
fi
PY="$(best_python || true)"
if [[ -z $PY && $family == dnf ]]; then
  # AlmaLinux / RHEL 8 and 9 ship 3.6 / 3.9 as python3, with newer ones alongside
  dnf install -y python3.12 python3.12-pip || dnf install -y python3.11 python3.11-pip
  PY="$(best_python || true)"
fi
[[ -n $PY ]] || { echo "RadPi needs Python 3.10 or newer, and none could be installed here"; exit 1; }
if [[ $family == apt ]]; then   # a newer python3.X than the default needs its own -venv package
  "$PY" -c 'import ensurepip' 2>/dev/null || apt-get install -y "$(basename "$PY")-venv"
fi
echo "   using $("$PY" --version) ($PY)"

# ---- service user and files
id radio &>/dev/null || useradd --system --home-dir "$DEST" --shell "$(command -v nologin || echo /sbin/nologin)" radio
mkdir -p "$DEST"
rsync -a --delete --exclude __pycache__ "$SRC/server" "$SRC/web" "$DEST/"
reqs_changed=1
cmp -s "$SRC/requirements.txt" "$DEST/requirements.txt" && reqs_changed=0
cp "$SRC/requirements.txt" "$SRC/requirements-bot.txt" "$DEST/"
if [[ ! -f $DEST/config.yaml ]]; then
  cp "$SRC/config.icom.yaml" "$DEST/config.yaml"
  echo "   new config: $DEST/config.yaml (from config.icom.yaml)"
fi
echo x86 > "$DEST/.platform"   # update.sh hands over to this script on this install

# ---- Python environment (made again if Python changed)
if [[ ! -x $DEST/.venv/bin/python ]] || ! "$DEST/.venv/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
  rm -rf "$DEST/.venv"
  "$PY" -m venv "$DEST/.venv"
  reqs_changed=1
fi
if (( reqs_changed )); then
  echo "== Python packages"
  "$DEST/.venv/bin/pip" install -q --upgrade pip
  "$DEST/.venv/bin/pip" install -q -r "$DEST/requirements.txt"
fi
if (( with_ai )) || [[ -e $DEST/.with-ai ]]; then   # asked for now, or added before
  echo "== the AI agent's packages (the first time is a large download, several minutes)"
  "$SRC/deploy/pip-ai.sh" "$DEST"
  touch "$DEST/.with-ai"
fi
# ---- HTTPS: a self-signed certificate until there's a real one (same files cert-cloudflare.sh writes)
TLS=$DEST/tls
if [[ ! -s $TLS/fullchain.pem || ! -s $TLS/privkey.pem ]]; then
  mkdir -p "$TLS"
  names="DNS:$(hostname),DNS:$(hostname).local,DNS:localhost,IP:127.0.0.1"
  fqdn="$(hostname -f 2>/dev/null || true)"
  [[ -n $fqdn && $fqdn != "$(hostname)" ]] && names+=",DNS:$fqdn"
  for ip in $(hostname -I 2>/dev/null || true); do [[ $ip == *:* ]] || names+=",IP:$ip"; done
  openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes -subj "/CN=$(hostname)" \
    -addext "subjectAltName=$names" -keyout "$TLS/privkey.pem" -out "$TLS/fullchain.pem" 2>/dev/null
  echo "   self-signed certificate made for $names"
fi
chown -R radio:radio "$DEST"
chmod 600 "$DEST/config.yaml" "$TLS/privkey.pem"
install -m 755 "$SRC/deploy/radpi-users" /usr/local/bin/radpi-users
# Fedora / RHEL sudo doesn't search /usr/local/bin (secure_path): link it where it does look
[[ -e /usr/bin/radpi-users ]] || ln -s /usr/local/bin/radpi-users /usr/bin/radpi-users

# ---- service: the Pi's unit, minus the Pi's gpio group (missing here, systemd won't start it),
# and allowed to use port 443 (ports below 1024 need it; the service doesn't run as root)
tmp="$(mktemp)"
sed -e 's/^SupplementaryGroups=.*$/SupplementaryGroups=audio/' \
    -e 's/^Group=radio$/Group=radio\nAmbientCapabilities=CAP_NET_BIND_SERVICE/' "$SRC/deploy/radpi.service" > "$tmp"
getent group audio >/dev/null || sed -i '/^SupplementaryGroups=/d' "$tmp"
if ! cmp -s "$tmp" "$UNIT"; then
  install -m 644 "$tmp" "$UNIT"
  systemctl daemon-reload
fi
rm -f "$tmp"
systemctl enable -q radpi

# ---- firewall: the radio's ports and the web port
port="$(cd "$DEST" && sudo -u radio "$DEST/.venv/bin/python" -c \
  'from server import config; print(config.load("config.yaml")["server"]["port"])' 2>/dev/null || echo 8080)"
# webrtc.udp_ports, if set: the range the audio uses ("50100-50149")
rtc="$(cd "$DEST" && sudo -u radio "$DEST/.venv/bin/python" -c \
  'from server import config, rtc; r = rtc.parse_ports(config.load("config.yaml")["webrtc"].get("udp_ports")); print(f"{r[0]}-{r[-1]}" if r else "")' 2>/dev/null || true)"
if systemctl is-active -q firewalld 2>/dev/null; then
  for p in 52001/tcp 50000/udp "$port/tcp" ${rtc:+$rtc/udp}; do firewall-cmd -q --permanent --add-port="$p"; done
  firewall-cmd -q --reload
  echo "   firewalld: opened 52001/tcp, 50000/udp (the radio), $port/tcp (the page)${rtc:+, $rtc/udp (WebRTC audio)}"
elif command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 52001/tcp >/dev/null; ufw allow 50000/udp >/dev/null; ufw allow "$port/tcp" >/dev/null
  [[ -n $rtc ]] && ufw allow "${rtc/-/:}/udp" >/dev/null
  echo "   ufw: opened 52001/tcp, 50000/udp (the radio), $port/tcp (the page)${rtc:+, $rtc/udp (WebRTC audio)}"
fi

if (( update )); then
  systemctl restart radpi
  sleep 2
  systemctl is-active -q radpi && echo "Updated and running. Reload the page in your browser." \
    || { echo "radpi did not start. Last log lines:"; journalctl -u radpi -n 30 --no-pager; exit 1; }
else
  echo
  echo "Installed. Next:"
  echo "  1. sudo nano $DEST/config.yaml           (icom.radio_ip, the presets; server.port)"
  echo "  2. sudo radpi-users add <your-callsign> --role control"
  echo "  3. sudo systemctl start radpi && journalctl -u radpi -f"
  echo "  4. program the radio's mic address as this machine's (TCP 52001 / UDP 50000 reach it)"
  echo "  5. browse to https://$(hostname)/ (self-signed: accept the warning once), or get a trusted"
  echo "     certificate: sudo ./deploy/cert-cloudflare.sh <name>"
  echo "To update later: sudo ./deploy/install-x86.sh   (or sudo ./deploy/update.sh)"
fi
