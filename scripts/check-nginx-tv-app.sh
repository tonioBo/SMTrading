#!/usr/bin/env bash
# Checks web/nginx: `nginx -t`, then the «SMTrading TV» app entry (:8087) against an echo
# "api" on a throwaway Docker network (nothing of the running stack is touched):
# only /api/tv-display/* is proxied, `X-SMT-Entry: tv-app` is set there and overwritten
# elsewhere, CORS for the app, home-network clients only, unknown Host closed.
# Usage: scripts/check-nginx-tv-app.sh   (needs docker, images nginx:stable + python:3.12-slim)
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NGINX_IMAGE="${NGINX_IMAGE:-nginx:stable}"
NET=smt-nginx-check
W="$(mktemp -d)"
cleanup() {
  docker rm -f smt-nginx-check-api smt-nginx-check-web >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  rm -rf "$W"
}
trap cleanup EXIT
mkdir -p "$W/certs" "$W/smt"

cat > "$W/echo.py" <<'EOF'
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def _r(self):
        body = json.dumps({"path": self.path,
                           "headers": {k.lower(): v for k, v in self.headers.items()}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_GET = do_POST = do_OPTIONS = _r
HTTPServer(("0.0.0.0", 8000), H).serve_forever()
EOF
openssl req -x509 -nodes -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout "$W/certs/smtrading.key" -out "$W/certs/smtrading.crt" -days 2 -subj /CN=check \
  2>/dev/null
cp "$W/certs/smtrading.crt" "$W/certs/smtrading-ca.crt"
echo 'server_name localhost 127.0.0.1 web 192.168.30.7;' > "$W/smt/server_names.conf"
echo 'set $smt_build "check";' > "$W/smt/build-id.conf"
for f in app.conf proxy-common.conf proxy-api.conf ca-cert.conf security-headers.conf tv-app.conf; do
  cp "$ROOT/web/nginx/$f" "$W/smt/"
done
chmod -R a+r "$W"

mounts=(-v "$ROOT/web/nginx/default.conf:/etc/nginx/conf.d/default.conf:ro"
        -v "$W/smt:/etc/nginx/smt:ro" -v "$W/certs:/etc/nginx/certs:ro")
docker network create "$NET" >/dev/null
docker run -d --name smt-nginx-check-api --network "$NET" --network-alias api \
  -v "$W/echo.py:/echo.py:ro" python:3.12-slim python /echo.py >/dev/null
docker run --rm --network "$NET" "${mounts[@]}" "$NGINX_IMAGE" nginx -t -q || {
  echo "nginx -t: configurazione non valida"; exit 1; }
docker run -d --name smt-nginx-check-web --network "$NET" -p 127.0.0.1:18087:8087 \
  -p 127.0.0.1:18080:8080 "${mounts[@]}" "$NGINX_IMAGE" >/dev/null
for _ in $(seq 30); do
  curl -s -o /dev/null http://127.0.0.1:18087/ && break
  sleep 0.3
done

H="Host: 192.168.30.7"; fail=0
ok() { if eval "$2"; then echo "OK  $1"; else echo "KO  $1"; fail=1; fi; }
entry() { python3 -c 'import json,sys; print(json.load(sys.stdin)["headers"].get("x-smt-entry"))'; }

r=$(curl -s -H "$H" -H "X-SMT-Entry: lan" -H "X-SMT-TV-App: 1" -H "X-SMT-TV-Token: t" \
  http://127.0.0.1:18087/api/tv-display/selection)
ok "8087 tags tv-app (a client value is overwritten)" '[ "$(echo "$r" | entry)" = tv-app ]'
ok "8087 forwards the app headers" \
  'echo "$r" | grep -q "\"x-smt-tv-token\": \"t\"" && echo "$r" | grep -q "\"x-smt-tv-app\": \"1\""'
r=$(curl -s -H "$H" -H "X-SMT-Entry: tv-app" http://127.0.0.1:18080/api/tv-display/selection)
ok "8080 overwrites a client tv-app with local" '[ "$(echo "$r" | entry)" = local ]'
for p in /api/tv/screens /api/auth/login /api/v1/x / /tv /index.html /assets/x.js /ws \
         /smtrading-ca.crt /api/tv-display /api/tv-display/../auth/me \
         /api/tv-display/..%2fauth/me; do
  c=$(curl -s --path-as-is -o /dev/null -w '%{http_code}' -H "$H" "http://127.0.0.1:18087$p")
  ok "8087 $p -> 404 ($c)" '[ "$c" = 404 ]'
done
c=$(curl -s -o /dev/null -w '%{http_code}' -H "$H" -X POST http://127.0.0.1:18087/api/tv-display/code)
ok "8087 POST /api/tv-display/code proxied ($c)" '[ "$c" = 200 ]'
h=$(curl -s -D - -o /dev/null -X OPTIONS -H "$H" -H "Origin: file://" \
  -H "Access-Control-Request-Headers: x-smt-tv-token" http://127.0.0.1:18087/api/tv-display/selection)
ok "preflight: 204 and CORS" 'echo "$h" | grep -q "^HTTP/1.1 204" &&
  echo "$h" | grep -qi "access-control-allow-headers: X-SMT-TV-Token, X-SMT-TV-App, Content-Type" &&
  echo "$h" | grep -qi "access-control-allow-origin: \*"'
h=$(curl -s -D - -o /dev/null -H "$H" http://127.0.0.1:18087/api/tv-display/health)
ok "GET: CORS, no-store, X-SMT-Build" 'echo "$h" | grep -qi "access-control-allow-origin: \*" &&
  echo "$h" | grep -qi "cache-control: no-store" && echo "$h" | grep -qi "x-smt-build: check"'
b=$(curl -s -H "$H" http://127.0.0.1:18087/tv-app/health)
ok "/tv-app/health" '[ "$b" = "{\"ok\":true,\"service\":\"smtrading-tv-app\"}" ]'
c=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: evil.example" http://127.0.0.1:18087/api/tv-display/selection)
ok "unknown Host closed ($c)" '[ "$c" = 000 ]'
c=$(docker exec smt-nginx-check-web curl -s -o /dev/null -w '%{http_code}' -H "$H" \
  http://127.0.0.1:8087/api/tv-display/selection)
ok "a non-private client (loopback) is denied ($c)" '[ "$c" = 403 ]'
c=$(docker exec smt-nginx-check-web curl -s -o /dev/null -w '%{http_code}' -H "$H" \
  http://127.0.0.1:8080/api/tv-display/selection)
ok "8080 keeps its own rules ($c)" '[ "$c" = 200 ]'
[ "$fail" = 0 ] && echo "nginx: tutto a posto" || echo "nginx: controlli falliti"
exit "$fail"
