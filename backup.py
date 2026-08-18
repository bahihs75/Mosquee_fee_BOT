from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

TABLES = [
    "users",
    "expense_requests",
    "request_versions",
    "workflow_events",
    "delivery_messages",
    "cashier_config",
    "admin_group_config",
    "app_settings",
]


def json_default(value):
    return str(value)


def create_backup(output_dir: str = "/var/data/backups") -> Path:
    database_url = os.environ["DATABASE_URL"]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"reimbursement_backup_{timestamp}.json.gz"
    payload = {
        "backup_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tables": {},
    }
    with psycopg2.connect(database_url) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        for table in TABLES:
            cur.execute(f"SELECT * FROM {table} ORDER BY 1")
            payload["tables"][table] = [dict(row) for row in cur.fetchall()]
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, default=json_default)
    return path


if __name__ == "__main__":
    print(create_backup())
