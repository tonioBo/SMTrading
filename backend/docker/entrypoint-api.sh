#!/bin/sh
set -e
# Task 3 adds: alembic upgrade head ; Task 10 adds: python -m smtrading.seed
exec uvicorn smtrading.api.main:create_app --factory --host 0.0.0.0 --port 8000
