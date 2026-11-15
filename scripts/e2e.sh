#!/usr/bin/env bash
# Playwright e2e on an isolated compose stack (project `smtrading-e2e`): its own images,
# volumes, network and a throwaway .env.e2e (random master key, database password and
# admin e2e@example.com with a random 20-character password, passed to Playwright).
# The stack is torn down with its volumes at the end, whatever happens. The main
# `smtrading` stack (and any other container) is never touched.
#
# «Denaro reale»: only this stack runs with SMT_LIVE_FAKE_BROKER=1 (live adapters talk to
# the in-memory FakeLiveBroker, the live gate stays on). Playwright runs twice: first every
# spec but `@live`, then `python -m smtrading.devtools.live_e2e_seed` (admin TOTP with a
# throwaway secret, Telegram «ready», the empty EUR portfolios) and the `@live` spec.
#
# «Prova generale»: with SMT_E2E_BATTERY=<file.jsonl>, scripts/order_battery.py runs between
# the seed and the `@live` spec (in a container of the e2e backend image, on the e2e network,
# as an MCP agent); one JSON line per test is appended to <file.jsonl> as it completes. A
# failed test fails the script at the end (the `@live` spec still runs).
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=smtrading-e2e
NETWORK=smtrading-e2e-net
# the only published port (127.0.0.1); 3100 is taken by paperclip
export SMT_E2E_WEB_PORT="${SMT_E2E_WEB_PORT:-3190}"
WEB_URL="http://127.0.0.1:$SMT_E2E_WEB_PORT"

# strip only a matching outer pair of quotes ("x" or 'x')
unquote() {
  local v="$1"
  if [[ ${#v} -ge 2 && ( ( $v == \"*\" ) || ( $v == \'*\' ) ) ]]; then
    v="${v:1:${#v}-2}"
  fi
  printf '%s' "$v"
}

random() { # $1 characters from [A-Za-z0-9] (or from the set $2)
  local out="" set="${2:-A-Za-z0-9}"
  while [ ${#out} -lt "$1" ]; do
    out+="$(head -c 64 /dev/urandom | tr -dc "$set")"
  done
  printf '%s' "${out:0:$1}"
}

PW_RAW="$(grep -oE '"@playwright/test": *"[^"]+"' web/package.json | head -1 | sed -E 's/.*: *//')"
PW_VERSION="$(unquote "$PW_RAW")"
IMAGE="mcr.microsoft.com/playwright:v${PW_VERSION}-jammy"

if curl -s -o /dev/null --max-time 2 "$WEB_URL/"; then
  echo "la porta $SMT_E2E_WEB_PORT è già in uso: imposta SMT_E2E_WEB_PORT" >&2
  exit 2
fi

tmpdir="$(mktemp -d)"
export SMT_E2E_ENV_FILE="$tmpdir/.env.e2e"
# Playwright's /work (node_modules installed once for both runs)
PW_VOLUME="smtrading-e2e-pw-work-$$"
compose() {
  docker compose -p "$PROJECT" --env-file "$SMT_E2E_ENV_FILE" \
    -f docker-compose.yml -f docker-compose.e2e.yml "$@"
}
cleanup() {
  local code=$?
  echo "smantello lo stack e2e..."
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  docker volume rm -f "$PW_VOLUME" >/dev/null 2>&1 || true
  rm -rf "$tmpdir"
  exit "$code"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

SMT_ADMIN_EMAIL="e2e@example.com"
SMT_ADMIN_PASSWORD="$(random 20)"
# throwaway TOTP secret for the live seed and the @live spec (base32)
SMT_E2E_TOTP_SECRET="$(random 32 'A-Z2-7')"
umask 077
cat > "$SMT_E2E_ENV_FILE" <<ENV
SMT_MASTER_KEY=$(random 48)
SMT_ADMIN_EMAIL=$SMT_ADMIN_EMAIL
SMT_ADMIN_PASSWORD=$SMT_ADMIN_PASSWORD
POSTGRES_PASSWORD=$(random 32)
SMT_TZ=Europe/Rome
SMT_LIVE_FAKE_BROKER=1
ENV
export SMT_ADMIN_EMAIL SMT_ADMIN_PASSWORD SMT_E2E_TOTP_SECRET

compose up -d --build --quiet-pull

# Public image: pull without the (possibly broken) credential helper if not cached.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  cfg="$tmpdir/docker"; mkdir -p "$cfg"; echo '{}' > "$cfg/config.json"
  DOCKER_CONFIG="$cfg" docker pull -q "$IMAGE" >/dev/null
fi

echo "attendo lo stack e2e (web + api)..."
ready=0
for _ in $(seq 1 90); do
  if [ "$(curl -s -o /dev/null -w '%{http_code}' "$WEB_URL/")" = "200" ] \
     && [ "$(curl -s -o /dev/null -w '%{http_code}' "$WEB_URL/api/auth/me")" = "401" ]; then
    ready=1; break
  fi
  sleep 2
done
[ "$ready" = 1 ] || { echo "stack e2e non pronto" >&2; compose logs --tail 50 >&2; exit 1; }

# On the e2e network against the web container's local entry (web:8080). Specs are
# mounted read-only; the pinned @playwright/test is installed once in the volume's /work.
playwright() { # $1: arguments of `npx playwright test`; the rest: extra `docker run` options
  docker run --rm --network "$NETWORK" --ipc=host \
    -e BASE_URL=http://web:8080 -e SMT_ADMIN_EMAIL -e SMT_ADMIN_PASSWORD -e PW_VERSION="$PW_VERSION" \
    "${@:2}" \
    -v "$PWD/web/e2e:/spec/e2e:ro" -v "$PWD/web/playwright.config.ts:/spec/playwright.config.ts:ro" \
    -v "$PW_VOLUME:/work" \
    "$IMAGE" bash -c '
      set -e
      cd /work && rm -rf e2e playwright.config.ts && cp -r /spec/e2e /spec/playwright.config.ts .
      if [ ! -d node_modules/@playwright/test ]; then
        npm init -y >/dev/null && npm i --no-audit --no-fund --silent "@playwright/test@${PW_VERSION}"
      fi
      npx playwright test '"$1"'
    '
}

playwright "--grep-invert @live"

echo "seed e2e del denaro reale (FakeLiveBroker)..."
compose exec -T -e SMT_E2E_TOTP_SECRET api python -m smtrading.devtools.live_e2e_seed

battery=0
if [ -n "${SMT_E2E_BATTERY:-}" ]; then
  echo "prova generale: batteria di ordini via MCP..."
  out_dir="$(cd "$(dirname "$SMT_E2E_BATTERY")" && pwd)"
  compose run --rm --no-deps -T --user "$(id -u):$(id -g)" --entrypoint python \
    -e SMT_E2E_TOTP_SECRET -v "$PWD/scripts:/scripts:ro" -v "$out_dir:/out" \
    api /scripts/order_battery.py --out "/out/$(basename "$SMT_E2E_BATTERY")" || battery=$?
fi

playwright "--grep @live" -e SMT_E2E_TOTP_SECRET
[ "$battery" = 0 ] || { echo "prova generale: alcune prove non sono andate (codice $battery)" >&2; exit 1; }
