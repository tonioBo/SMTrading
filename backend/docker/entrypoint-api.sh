#!/bin/sh
set -e
# Task 10 adds: python -m smtrading.seed
alembic upgrade head
exec uvicorn smtrading.api.main:create_app --factory --host 0.0.0.0 --port 8000
