"""SQLite persistence layer.

Tables
------
rulesets          one row per rule set (identity, current version pointer)
ruleset_versions  immutable rule content; an update inserts a new version
documents         submitted documents
decisions         computed results, bound to (ruleset, version, document);
                  old results remain queryable forever
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS rulesets (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT,
    current_version INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ruleset_versions (
    ruleset_id      TEXT NOT NULL REFERENCES rulesets(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    conflict_policy TEXT NOT NULL,
    rules_json      TEXT NOT NULL,
    note            TEXT,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (ruleset_id, version)
);

CREATE TABLE IF NOT EXISTS documents (
    id          TEXT PRIMARY KEY,
    external_id TEXT,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id          TEXT PRIMARY KEY,
    ruleset_id  TEXT NOT NULL,
    version     INTEGER NOT NULL,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    result_json TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (ruleset_id, version)
        REFERENCES ruleset_versions(ruleset_id, version)
);

CREATE INDEX IF NOT EXISTS idx_decisions_ruleset
    ON decisions(ruleset_id, version);
CREATE INDEX IF NOT EXISTS idx_decisions_document ON decisions(document_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


def get_db_path() -> Path:
    return Path(os.environ.get("RULES_DB_PATH", "/data/rules.db"))


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Rule sets & versions
# --------------------------------------------------------------------------- #


def create_ruleset(
    conn: sqlite3.Connection,
    *,
    name: str,
    description: str | None,
    conflict_policy: str,
    rules: list[dict[str, Any]],
    note: str | None = "initial version",
) -> dict[str, Any]:
    now = utcnow()
    rs_id = new_id()
    conn.execute(
        "INSERT INTO rulesets (id, name, description, current_version, created_at, updated_at)"
        " VALUES (?, ?, ?, 1, ?, ?)",
        (rs_id, name, description, now, now),
    )
    conn.execute(
        "INSERT INTO ruleset_versions (ruleset_id, version, conflict_policy, rules_json, note, created_at)"
        " VALUES (?, 1, ?, ?, ?, ?)",
        (rs_id, conflict_policy, json.dumps(rules, ensure_ascii=False), note, now),
    )
    conn.commit()
    return get_ruleset(conn, rs_id)


def get_ruleset(conn: sqlite3.Connection, ruleset_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM rulesets WHERE id = ?", (ruleset_id,)).fetchone()
    return dict(row) if row else None


def list_rulesets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM rulesets ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def get_version(
    conn: sqlite3.Connection, ruleset_id: str, version: int | None = None
) -> dict[str, Any] | None:
    if version is None:
        rs = get_ruleset(conn, ruleset_id)
        if rs is None:
            return None
        version = rs["current_version"]
    row = conn.execute(
        "SELECT * FROM ruleset_versions WHERE ruleset_id = ? AND version = ?",
        (ruleset_id, version),
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["rules"] = json.loads(data.pop("rules_json"))
    return data


def list_versions(conn: sqlite3.Connection, ruleset_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT ruleset_id, version, conflict_policy, note, created_at,"
        " json_array_length(rules_json) AS rule_count"
        " FROM ruleset_versions WHERE ruleset_id = ? ORDER BY version",
        (ruleset_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def add_version(
    conn: sqlite3.Connection,
    ruleset_id: str,
    *,
    conflict_policy: str,
    rules: list[dict[str, Any]],
    name: str | None = None,
    description: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    rs = get_ruleset(conn, ruleset_id)
    if rs is None:
        raise KeyError(ruleset_id)
    new_version = rs["current_version"] + 1
    now = utcnow()
    conn.execute(
        "INSERT INTO ruleset_versions (ruleset_id, version, conflict_policy, rules_json, note, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            ruleset_id,
            new_version,
            conflict_policy,
            json.dumps(rules, ensure_ascii=False),
            note or f"edit -> v{new_version}",
            now,
        ),
    )
    conn.execute(
        "UPDATE rulesets SET current_version = ?, name = COALESCE(?, name),"
        " description = COALESCE(?, description), updated_at = ? WHERE id = ?",
        (new_version, name, description, now, ruleset_id),
    )
    conn.commit()
    return get_version(conn, ruleset_id, new_version)


# --------------------------------------------------------------------------- #
# Documents & decisions
# --------------------------------------------------------------------------- #


def insert_document(conn: sqlite3.Connection, content: dict[str, Any], external_id: str | None) -> dict[str, Any]:
    doc_id = new_id()
    now = utcnow()
    conn.execute(
        "INSERT INTO documents (id, external_id, content, created_at) VALUES (?, ?, ?, ?)",
        (doc_id, external_id, json.dumps(content, ensure_ascii=False), now),
    )
    conn.commit()
    return {"id": doc_id, "external_id": external_id, "content": content, "created_at": now}


def get_document(conn: sqlite3.Connection, document_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["content"] = json.loads(data["content"])
    return data


def save_decision(
    conn: sqlite3.Connection,
    *,
    ruleset_id: str,
    version: int,
    document_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    decision_id = new_id()
    now = utcnow()
    conn.execute(
        "INSERT INTO decisions (id, ruleset_id, version, document_id, result_json, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (decision_id, ruleset_id, version, document_id, json.dumps(result, ensure_ascii=False), now),
    )
    conn.commit()
    return {
        "id": decision_id,
        "ruleset_id": ruleset_id,
        "version": version,
        "document_id": document_id,
        "result": result,
        "created_at": now,
    }


def get_decision(conn: sqlite3.Connection, decision_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["result"] = json.loads(data.pop("result_json"))
    return data


def list_decisions(
    conn: sqlite3.Connection,
    ruleset_id: str,
    *,
    version: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if version is None:
        rows = conn.execute(
            "SELECT id, ruleset_id, version, document_id, created_at FROM decisions"
            " WHERE ruleset_id = ? ORDER BY created_at DESC LIMIT ?",
            (ruleset_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, ruleset_id, version, document_id, created_at FROM decisions"
            " WHERE ruleset_id = ? AND version = ? ORDER BY created_at DESC LIMIT ?",
            (ruleset_id, version, limit),
        ).fetchall()
    return [dict(r) for r in rows]
