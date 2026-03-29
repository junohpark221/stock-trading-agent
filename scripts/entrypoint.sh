#!/bin/bash
set -e

echo "=== Running Alembic migrations ==="
alembic upgrade head

echo "=== Starting uvicorn ==="
exec uvicorn src.main:app --host 0.0.0.0 --port 8000 --workers 1 --log-level info
