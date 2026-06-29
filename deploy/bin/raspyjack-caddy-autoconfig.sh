#!/usr/bin/env bash
# RaspyJack: generate Caddyfile + long-lived self-signed cert
# Binds on 0.0.0.0 — works on any network without reconfiguration
set -euo pipefail

CERT_DIR=/etc/caddy/certs
CERT=$CERT_DIR/raspyjack.crt
KEY=$CERT_DIR/raspyjack.key

# Generate 10-year self-signed cert covering all common private IPs
# Only regenerate if cert doesn't exist or is older than 1 year
if [ ! -f "$CERT" ] || [ ! -f "$KEY" ] || \
   [ "$(find "$CERT" -mtime +365 2>/dev/null)" ]; then
  mkdir -p "$CERT_DIR"

  # Collect all current IPs for SAN
  SAN="IP:127.0.0.1,IP:0.0.0.0,DNS:raspyjack,DNS:raspyjack.local,DNS:localhost"
  for iface in $(ls /sys/class/net/); do
    case "$iface" in lo|docker*|veth*|br-*) continue ;; esac
    IP=$(ip -4 -o addr show "$iface" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)
    [ -n "$IP" ] && SAN="$SAN,IP:$IP"
  done

  openssl req -x509 -nodes -days 3650 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
    -keyout "$KEY" -out "$CERT" \
    -subj "/CN=RaspyJack" \
    -addext "subjectAltName=$SAN" 2>/dev/null

  chown caddy:caddy "$KEY" 2>/dev/null || true
  echo "[raspyjack-caddy] Generated new TLS cert with SAN: $SAN"
fi

cat > /etc/caddy/Caddyfile <<EOF
:443 {
    tls $CERT $KEY

    @ws path /ws*
    reverse_proxy @ws 127.0.0.1:8765
    reverse_proxy 127.0.0.1:8080
}
EOF

systemctl reload caddy 2>/dev/null || systemctl restart caddy
echo "[raspyjack-caddy] Bound to 0.0.0.0 (all interfaces)"
