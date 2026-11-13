#!/bin/sh
# nginx image entrypoint hook: the local CA + server certificate and the Host allowlist.
#
# - /etc/nginx/certs (volume web_certs) keeps smtrading-ca.{crt,key} (local CA, ECDSA
#   P-256, 10 years) and smtrading.{crt,key} (server certificate signed by it, 825 days,
#   SAN = localhost, 127.0.0.1 + SMT_PUBLIC_HOSTS). Existing files are kept; the server
#   certificate is re-issued (same CA: nothing to reinstall) when SMT_PUBLIC_HOSTS changes
#   or it expires within 30 days.
# - /etc/nginx/smt/server_names.conf: `server_name` with the same hosts plus `web` (e2e).
set -eu

CERTS=/etc/nginx/certs
CONF=/etc/nginx/smt
HOSTS="${SMT_PUBLIC_HOSTS:-192.168.30.7}"
mkdir -p "$CERTS" "$CONF"

san="DNS:localhost,IP:127.0.0.1"
names="localhost 127.0.0.1 web"
for h in $(echo "$HOSTS" | tr ',' ' '); do
  case "$h" in
    *[!0-9A-Za-z.:-]*) echo "SMT_PUBLIC_HOSTS: host non valido: $h" >&2; exit 1 ;;
  esac
  if echo "$h" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$|:'; then
    san="$san,IP:$h"
  else
    san="$san,DNS:$h"
  fi
  names="$names $h"
done
echo "server_name $names;" > "$CONF/server_names.conf"

if [ ! -s "$CERTS/smtrading-ca.key" ] || [ ! -s "$CERTS/smtrading-ca.crt" ]; then
  echo "smtrading: genero la CA locale" >&2
  openssl req -x509 -new -nodes -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
    -keyout "$CERTS/smtrading-ca.key" -out "$CERTS/smtrading-ca.crt" -days 3650 \
    -subj "/CN=SMTrading CA locale" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
  rm -f "$CERTS/smtrading.crt" "$CERTS/smtrading.key" "$CERTS/smtrading.san"
fi

if [ ! -s "$CERTS/smtrading.crt" ] || [ ! -s "$CERTS/smtrading.key" ] \
   || [ "$(cat "$CERTS/smtrading.san" 2>/dev/null)" != "$san" ] \
   || ! openssl x509 -checkend 2592000 -noout -in "$CERTS/smtrading.crt" >/dev/null 2>&1; then
  echo "smtrading: genero il certificato del server ($san)" >&2
  tmp="$(mktemp -d)"
  printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=%s\n' "$san" > "$tmp/ext"
  openssl req -new -nodes -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
    -keyout "$CERTS/smtrading.key" -out "$tmp/csr" -subj "/CN=SMTrading" 2>/dev/null
  openssl x509 -req -in "$tmp/csr" -CA "$CERTS/smtrading-ca.crt" -CAkey "$CERTS/smtrading-ca.key" \
    -CAcreateserial -out "$CERTS/smtrading.crt" -days 825 -sha256 -extfile "$tmp/ext" 2>/dev/null
  rm -rf "$tmp"
  echo "$san" > "$CERTS/smtrading.san"
fi
chmod 600 "$CERTS/smtrading.key" "$CERTS/smtrading-ca.key"
chmod 644 "$CERTS/smtrading.crt" "$CERTS/smtrading-ca.crt"
