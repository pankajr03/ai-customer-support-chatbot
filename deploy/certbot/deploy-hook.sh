#!/bin/sh
# certbot deploy hook: install a newly issued or renewed certificate for Nginx and
# reload Nginx without downtime. See docs/https-setup.md.
#
# certbot runs it (as root) after every successful issuance or renewal, with
# RENEWED_LINEAGE=/etc/letsencrypt/live/<name>:
#   certbot certonly ... --deploy-hook /srv/support-chatbot/deploy/certbot/deploy-hook.sh
#
# Steps: copy fullchain.pem and privkey.pem (resolving certbot's symlinks) into
# deploy/nginx/certs/; refuse an expired certificate or a key that does not match it;
# keep the previous pair; check the Nginx configuration with the new files, restoring the
# previous pair if the check fails; then reload Nginx gracefully (open connections finish
# on the old certificate, new ones get the new one; nothing restarts).
#
# Settings (environment):
#   DEPLOY_DIR     the deploy/ folder (default: this script's parent folder)
#   NGINX_GID      group that may read the key inside the container (default 101, Nginx)
#   KEY_MODE       mode of the installed private key (default 640). Development only:
#                  644 when the files cannot be given to group 101 (not running as root).
set -eu

DEPLOY_DIR="${DEPLOY_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
LINEAGE="${RENEWED_LINEAGE:?RENEWED_LINEAGE is not set: run this as a certbot deploy hook}"
CERTS="$DEPLOY_DIR/nginx/certs"
NGINX_GID="${NGINX_GID:-101}"
KEY_MODE="${KEY_MODE:-640}"

log() { echo "deploy-hook: $*"; }
fail() { echo "deploy-hook: $*" >&2; exit 1; }

[ -d "$CERTS" ] || fail "$CERTS does not exist"
staging=$(mktemp -d "$CERTS/.incoming.XXXXXX")
trap 'rm -rf "$staging"' EXIT

# live/<name>/*.pem are symlinks into archive/: copy the contents.
cp -L "$LINEAGE/fullchain.pem" "$staging/fullchain.pem"
cp -L "$LINEAGE/privkey.pem" "$staging/privkey.pem"

openssl x509 -in "$staging/fullchain.pem" -noout -checkend 0 >/dev/null \
  || fail "the new certificate has already expired: not installed"
cert_key=$(openssl x509 -in "$staging/fullchain.pem" -noout -pubkey | openssl sha256)
private_key=$(openssl pkey -in "$staging/privkey.pem" -pubout | openssl sha256)
[ "$cert_key" = "$private_key" ] || fail "the private key does not match the certificate: not installed"

if [ "$(id -u)" = 0 ]; then
  chown "0:$NGINX_GID" "$staging/fullchain.pem" "$staging/privkey.pem"
fi
chmod 644 "$staging/fullchain.pem"
chmod "$KEY_MODE" "$staging/privkey.pem"

# Keep the previous pair, then swap the new one in (each mv is atomic).
mkdir -p "$CERTS/previous"
for f in fullchain.pem privkey.pem; do
  [ -f "$CERTS/$f" ] && cp -p "$CERTS/$f" "$CERTS/previous/$f"
done
mv -f "$staging/fullchain.pem" "$CERTS/fullchain.pem"
mv -f "$staging/privkey.pem" "$CERTS/privkey.pem"

cd "$DEPLOY_DIR"
if ! docker compose exec -T nginx nginx -t -q -c /tmp/nginx.conf; then
  log "Nginx rejected the new files: restoring the previous certificate"
  cp -p "$CERTS/previous/fullchain.pem" "$CERTS/fullchain.pem"
  cp -p "$CERTS/previous/privkey.pem" "$CERTS/privkey.pem"
  exit 1
fi
docker compose exec -T nginx nginx -c /tmp/nginx.conf -s reload
log "installed $(openssl x509 -in "$CERTS/fullchain.pem" -noout -subject -enddate | tr '\n' ' ')and reloaded Nginx"
