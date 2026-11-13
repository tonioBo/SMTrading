#!/usr/bin/env bash
# Playwright e2e against the compose stack, run in the official Playwright image.
# Uses SMT_ADMIN_EMAIL / SMT_ADMIN_PASSWORD from .env (never printed).
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "manca .env" >&2; exit 2; }
# export only the two admin variables, without echoing them
SMT_ADMIN_EMAIL="$(grep -E '^SMT_ADMIN_EMAIL=' .env | head -1 | cut -d= -f2- | tr -d '\r"' | sed "s/^'//;s/'$//")"
SMT_ADMIN_PASSWORD="$(grep -E '^SMT_ADMIN_PASSWORD=' .env | head -1 | cut -d= -f2- | tr -d '\r"' | sed "s/^'//;s/'$//")"
export SMT_ADMIN_EMAIL SMT_ADMIN_PASSWORD
if [ -z "$SMT_ADMIN_EMAIL" ] || [ -z "$SMT_ADMIN_PASSWORD" ]; then
  echo "SMT_ADMIN_EMAIL/SMT_ADMIN_PASSWORD vuoti: controlla SMT_ADMIN_* in .env" >&2
  exit 2
fi

PW_VERSION="$(node -p "require('./web/package.json').devDependencies['@playwright/test']" 2>/dev/null \
  || grep -oP '"@playwright/test": "\K[^"]+' web/package.json)"
IMAGE="mcr.microsoft.com/playwright:v${PW_VERSION}-jammy"

docker compose up -d --build

# Public image: pull without the (possibly broken) credential helper if not cached.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  tmpcfg="$(mktemp -d)"; echo '{}' > "$tmpcfg/config.json"
  DOCKER_CONFIG="$tmpcfg" docker pull "$IMAGE"; rm -rf "$tmpcfg"
fi

echo "attendo /health e web..."
for i in $(seq 1 60); do
  if curl -fs http://127.0.0.1:8000/health >/dev/null 2>&1 \
     && [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3000/)" = "200" ]; then
    ready=1; break
  fi
  sleep 2
done
[ "${ready:-0}" = 1 ] || { echo "stack non pronto" >&2; exit 1; }

# Run on the compose network against the web container (host networking does not
# reach 127.0.0.1 ports of the host on Docker Desktop). Specs are mounted read-only and
# the pinned @playwright/test is installed in the container's own /work (the host's
# node_modules are not touched).
set +e
docker run --rm --network smtrading-net --ipc=host \
  -e BASE_URL=http://web:80 -e SMT_ADMIN_EMAIL -e SMT_ADMIN_PASSWORD -e PW_VERSION="$PW_VERSION" \
  -v "$PWD/web/e2e:/spec/e2e:ro" -v "$PWD/web/playwright.config.ts:/spec/playwright.config.ts:ro" \
  "$IMAGE" bash -c '
    set -e
    mkdir -p /work && cd /work && cp -r /spec/e2e /spec/playwright.config.ts .
    npm init -y >/dev/null && npm i --no-audit --no-fund --silent "@playwright/test@${PW_VERSION}"
    npx playwright test
  '
code=$?
set -e
exit $code
