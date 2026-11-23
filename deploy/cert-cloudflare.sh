#!/usr/bin/env bash
# A free, browser-trusted HTTPS certificate for RadPi from Let's Encrypt, proved with a DNS record
# set through Cloudflare's API (DNS-01): no port 80 to open, and it works on a private address too.
# Renewal is automatic (certbot's timer), and each new certificate is copied where RadPi can read
# it and RadPi restarted.
#
#   sudo ./deploy/cert-cloudflare.sh radpi.example.com [you@example.com]
#
# Needs a Cloudflare API token that can edit DNS in the name's zone (My Profile > API Tokens >
# "Edit zone DNS"). It's read from /etc/cf-ddns.conf (CF_API_TOKEN=..., as the Cloudflare DDNS
# updater uses), or asked for. The e-mail address is optional: Let's Encrypt uses it only to warn
# about expiry problems.
#
# Then in /opt/radpi/config.yaml (the script prints these):
#   server:
#     tls_cert: /opt/radpi/tls/fullchain.pem
#     tls_key: /opt/radpi/tls/privkey.pem
set -euo pipefail
renew_timer=""

name="${1:-}"
email="${2:-}"
[[ -n $name ]] || { sed -n '7p' "$0" | sed 's/^# *//'; exit 1; }
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

DEST=/opt/radpi
TLS=$DEST/tls
CREDS=/etc/letsencrypt/cloudflare-radpi.ini
HOOK=/etc/letsencrypt/renewal-hooks/deploy/radpi.sh

echo "== certbot and its Cloudflare plugin"
# Is the Cloudflare plugin there? (certbot lists its installed plugins)
have_plugin() { command -v certbot >/dev/null && certbot plugins 2>/dev/null | grep -q dns-cloudflare; }
if ! have_plugin; then
  if command -v apt-get >/dev/null; then                 # Debian, Ubuntu, Raspberry Pi OS
    apt-get update -qq
    apt-get install -y certbot python3-certbot-dns-cloudflare
  elif command -v dnf >/dev/null; then                   # Fedora; AlmaLinux / Rocky / RHEL need EPEL
    . /etc/os-release
    if [[ " ${ID:-} ${ID_LIKE:-} " != *" fedora "* || " ${ID:-} ${ID_LIKE:-} " == *" rhel "* ]]; then
      rpm -q epel-release >/dev/null 2>&1 || dnf install -y epel-release
    fi
    dnf install -y certbot python3-certbot-dns-cloudflare
  else
    echo "Install certbot and its Cloudflare DNS plugin (python3-certbot-dns-cloudflare), then run this again"
    exit 1
  fi
fi
have_plugin || { echo "certbot's Cloudflare plugin still isn't available"; exit 1; }
# Renewal: Debian and Ubuntu switch certbot's timer on when it's installed; Fedora / RHEL don't
for t in certbot-renew.timer certbot.timer; do
  if systemctl list-unit-files "$t" --no-legend 2>/dev/null | grep -q .; then
    systemctl enable -q --now "$t"
    renew_timer=$t
    break
  fi
done

# ---- the API token, kept root-only
mkdir -p /etc/letsencrypt
if [[ ! -s $CREDS ]]; then
  token=""
  [[ -r /etc/cf-ddns.conf ]] && token="$(sed -n 's/^CF_API_TOKEN=//p' /etc/cf-ddns.conf | head -1)"
  if [[ -z $token ]]; then
    read -rsp "Cloudflare API token (Edit zone DNS): " token; echo
  else
    echo "   using the API token from /etc/cf-ddns.conf"
  fi
  [[ -n $token ]] || { echo "no token"; exit 1; }
  install -m 600 /dev/null "$CREDS"
  printf 'dns_cloudflare_api_token = %s\n' "$token" > "$CREDS"
fi

# ---- after every issue and renewal: copy for RadPi (the service user can't read /etc/letsencrypt)
mkdir -p "$(dirname "$HOOK")"
cat > "$HOOK" <<EOF
#!/bin/sh
# Installed by RadPi's deploy/cert-cloudflare.sh: give RadPi the new certificate for $name.
set -e
case " \$RENEWED_DOMAINS " in *" $name "*) ;; *) exit 0 ;; esac
install -d -m 750 -o radio -g radio $TLS
install -m 644 -o radio -g radio "\$RENEWED_LINEAGE/fullchain.pem" $TLS/fullchain.pem
install -m 600 -o radio -g radio "\$RENEWED_LINEAGE/privkey.pem" $TLS/privkey.pem
systemctl try-restart radpi || true
EOF
chmod 755 "$HOOK"

echo "== asking Let's Encrypt for $name (takes about a minute: the DNS record has to spread)"
contact=(--register-unsafely-without-email)
[[ -n $email ]] && contact=(-m "$email")
certbot certonly --non-interactive --agree-tos "${contact[@]}" \
  --dns-cloudflare --dns-cloudflare-credentials "$CREDS" --dns-cloudflare-propagation-seconds 30 \
  -d "$name"

# the hook (in certbot's renewal-hooks folder) runs on renewals; for this first one, run it here
RENEWED_DOMAINS="$name" RENEWED_LINEAGE="/etc/letsencrypt/live/$name" "$HOOK"

echo
echo "Done: $TLS/fullchain.pem and privkey.pem, renewed automatically${renew_timer:+ ($renew_timer)}."
echo "In $DEST/config.yaml, under server:"
echo "    tls_cert: $TLS/fullchain.pem"
echo "    tls_key: $TLS/privkey.pem"
echo "then: sudo systemctl restart radpi, and browse to https://$name:<server.port>"
