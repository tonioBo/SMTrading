#!/bin/sh
set -e
exec uvicorn smtrading.api.main:create_app --factory --host 0.0.0.0 --port 8000
