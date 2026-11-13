#!/usr/bin/env bash
# Connects the Paperclip container to the SMTrading network (idempotent).
# Usage: scripts/connect-paperclip.sh [container-name]   (default: paperclip)
set -euo pipefail

NETWORK="smtrading-net"
CONTAINER="${1:-paperclip}"

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "container '$CONTAINER' not found" >&2
  exit 1
fi

if docker inspect -f '{{range $k, $_ := .NetworkSettings.Networks}}{{println $k}}{{end}}' "$CONTAINER" \
  | grep -qx "$NETWORK"; then
  echo "'$CONTAINER' is already on $NETWORK"
  exit 0
fi

docker network connect "$NETWORK" "$CONTAINER"
echo "'$CONTAINER' connected to $NETWORK"
