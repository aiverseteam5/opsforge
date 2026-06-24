#!/bin/sh
# Single image, three entrypoints (doctrine #2: same codebase, different command).
set -e

case "$1" in
  migrate)
    exec alembic upgrade head
    ;;
  api)
    exec uvicorn opsforge.main:app --host 0.0.0.0 --port 8080
    ;;
  worker)
    exec python -m opsforge.worker
    ;;
  *)
    exec "$@"
    ;;
esac
