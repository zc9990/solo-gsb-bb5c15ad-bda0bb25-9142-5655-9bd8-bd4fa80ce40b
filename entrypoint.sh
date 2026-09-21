#!/usr/bin/env bash
set -euo pipefail

# Pick the available interpreter ("python" in Docker, often "python3" locally).
if command -v python >/dev/null 2>&1; then
  PY=python
else
  PY=python3
fi

# Create / migrate the SQLite schema (idempotent) and optionally seed demo data.
$PY - <<'PYEOF'
from app import store
store.init_db()
print("Database initialised at", store.get_db_path())
PYEOF

if [[ "${SEED_DEMO_DATA:-false}" == "true" ]]; then
  $PY - <<'PYEOF'
from app import seed, store
conn = store.connect()
print(seed.seed_if_empty(conn))
conn.close()
PYEOF
fi

exec $PY -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
