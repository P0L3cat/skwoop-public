#!/bin/bash
# One-time local TLS for the web pod (browsers only allow the mic on HTTPS). Makes a small local CA (10 years) and a
# server certificate for this computer's names and addresses (825 days, the longest iOS accepts), in WEBPOD_TLS_DIR.
# HOST is the certificate DNS name. Nothing is appended to it.
# Re-run to add an address or renew the server cert; the CA is kept, so phones that trust it keep trusting.
#   webpod/make_cert.sh [extra-ip-or-name ...]
set -euo pipefail
DIR="${WEBPOD_TLS_DIR:-$HOME/.skwoop/webpod-tls}"
mkdir -p "$DIR" && chmod 700 "$DIR" && cd "$DIR"
umask 077

if [ ! -f ca.key ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -days 3650 -sha256 \
    -subj "/CN=Skwoop web pod local CA/O=Skwoop" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
fi

host="${HOST:-$(hostname -s 2>/dev/null || echo skwoop)}"
san="DNS:${host},DNS:localhost,IP:127.0.0.1"
for ifc in en0 en1 en2; do
  ip="$(ipconfig getifaddr "$ifc" 2>/dev/null || true)"
  [ -n "$ip" ] && san="$san,IP:$ip"
done
for extra in "$@"; do
  if [[ "$extra" =~ ^[0-9.]+$ ]]; then san="$san,IP:$extra"; else san="$san,DNS:$extra"; fi
done

cat > server.ext <<EXT
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=$san
EXT
openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr -subj "/CN=${host}" 2>/dev/null
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -days 825 -sha256 \
  -extfile server.ext 2>/dev/null
rm -f server.csr
chmod 600 ca.key server.key
chmod 644 ca.crt server.crt
echo "server cert for: $san"
openssl x509 -in server.crt -noout -enddate
