#!/bin/sh
set -e
alembic upgrade head
python -m smtrading.seed
exec uvicorn smtrading.api.main:create_app --factory --host 0.0.0.0 --port 8000
