#!/bin/sh
# ============================================================================
#  DEVELOPMENT ONLY. Creates a short-lived SELF-SIGNED certificate for local
#  testing of the Docker stack (deploy/nginx/certs/ by default).
#
#  It is not trusted by browsers or by any client without being imported, and it
#  is NOT evidence that public HTTPS works. Public HTTPS needs a certificate from a
#  trusted certificate authority (docs/https-setup.md).
# ============================================================================
#
# Usage (from deploy/):
#   sh dev/make-dev-certificate.sh                     # localhost
#   sh dev/make-dev-certificate.sh localhost admin.localhost
#   OUT_DIR=/tmp/lineage sh dev/make-dev-certificate.sh localhost   # elsewhere
#
# The subject names it as a development certificate, it is valid for 7 days, and the
# private key is readable by everyone (KEY_MODE=644): fine on a laptop, never on a
# server.
set -eu

OUT_DIR="${OUT_DIR:-$(cd "$(dirname "$0")/.." && pwd)/nginx/certs}"
KEY_MODE="${KEY_MODE:-644}"
[ "$#" -gt 0 ] || set -- localhost

san=""
for name in "$@"; do
  san="${san:+$san,}DNS:$name"
done

mkdir -p "$OUT_DIR"
openssl req -x509 -newkey rsa:2048 -nodes -days 7 \
  -subj "/O=DEVELOPMENT ONLY - not trusted/CN=$1" \
  -addext "subjectAltName=$san" \
  -keyout "$OUT_DIR/privkey.pem" -out "$OUT_DIR/fullchain.pem" 2>/dev/null
chmod 644 "$OUT_DIR/fullchain.pem"
chmod "$KEY_MODE" "$OUT_DIR/privkey.pem"
echo "DEVELOPMENT ONLY certificate for $* in $OUT_DIR (7 days, self-signed, untrusted):"
openssl x509 -in "$OUT_DIR/fullchain.pem" -noout -subject -enddate
