#!/bin/sh
set -e
# X-Forwarded-Proto/For are trusted only from the networks this container is attached to
# (the compose network, where nginx runs): see smtrading/api/proxies.py.
FORWARDED_ALLOW_IPS="$(python -m smtrading.api.proxies)"
exec uvicorn smtrading.api.main:create_app --factory --host 0.0.0.0 --port 8000 \
  --proxy-headers --forwarded-allow-ips "$FORWARDED_ALLOW_IPS"
