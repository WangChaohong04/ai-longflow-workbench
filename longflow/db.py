"""SQLite 连接、建表与基础工具（WAL，幂等迁移）。"""
from __future__ import annotations

import os
import sqlite3
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from . import models as M

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  parent_id TEXT,
  root_id TEXT NOT NULL,
  title TEXT NOT NULL,
  kind TEXT NOT NULL,
  agent_role TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  objective TEXT NOT NULL,
  slots_json TEXT NOT NULL DEFAULT '{}',
  plan_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT NOT NULL DEFAULT '{}',
  depends_on_json TEXT NOT NULL DEFAULT '[]',
  idempotency_key TEXT,
  scheduled_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_root ON tasks(root_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  args_json TEXT NOT NULL,
  args_hash TEXT NOT NULL,
  reason TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  decided_by TEXT,
  decided_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approvals_task ON approvals(task_id);

CREATE TABLE IF NOT EXISTS grants (
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL,
  tool_name TEXT,
  action TEXT,
  object_pattern TEXT,
  max_count INTEGER,
  used_count INTEGER DEFAULT 0,
  expires_at TEXT,
  granted_by TEXT,
  task_id TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_sources (
  id TEXT PRIMARY KEY,
  name TEXT,
  scenario TEXT,
  version TEXT,
  source_path TEXT,
  access_scope TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
  id TEXT PRIMARY KEY,
  source_id TEXT,
  doc_name TEXT,
  section TEXT,
  text TEXT,
  fields_json TEXT DEFAULT '{}',
  citations_json TEXT DEFAULT '[]',
  scenario TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_scenario ON knowledge_chunks(scenario);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  task_id TEXT,
  kind TEXT NOT NULL,
  actor TEXT,
  detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);

CREATE TABLE IF NOT EXISTS eval_runs (
  id TEXT PRIMARY KEY,
  ts TEXT,
  case_id TEXT,
  passed INTEGER,
  checks_json TEXT,
  note TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn) -> sqlite3.Connection:
    """初始化数据库。接受已建连接，或数据库文件路径（返回连接）。"""
    if isinstance(conn, str):
        conn = connect(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def reset_db(db_path: str) -> None:
    """删除数据库文件（含 WAL 侧车文件）。"""
    for suffix in ("", "-wal", "-shm"):
        p = Path(db_path + suffix)
        if p.exists():
            p.unlink()


# ---- 任务行 CRUD 辅助 ----

def insert_task(
    conn: sqlite3.Connection,
    *,
    title: str,
    kind: str,
    agent_role: str,
    objective: str,
    root_id: str,
    parent_id: str | None = None,
    depends_on: list[str] | None = None,
    slots: dict | None = None,
    status: str = M.PENDING,
) -> str:
    import json

    tid = new_id("t")
    ts = now()
    conn.execute(
        """INSERT INTO tasks (id, parent_id, root_id, title, kind, agent_role, status,
           objective, slots_json, plan_json, result_json, depends_on_json, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            tid,
            parent_id,
            root_id or tid,
            title,
            kind,
            agent_role,
            status,
            objective,
            json.dumps(slots or {}, ensure_ascii=False),
            "{}",
            "{}",
            json.dumps(depends_on or [], ensure_ascii=False),
            ts,
            ts,
        ),
    )
    conn.commit()
    return tid


def get_task(conn: sqlite3.Connection, task_id: str) -> M.Task | None:
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return M.Task.from_row(row) if row else None


def list_tasks(conn: sqlite3.Connection, *, root_only: bool = False) -> list[M.Task]:
    sql = "SELECT * FROM tasks"
    if root_only:
        sql += " WHERE parent_id IS NULL"
    sql += " ORDER BY created_at"
    rows = conn.execute(sql).fetchall()
    return [M.Task.from_row(r) for r in rows]


def list_children(conn: sqlite3.Connection, root_id: str) -> list[M.Task]:
    rows = conn.execute(
        "SELECT * FROM tasks WHERE root_id=? AND id!=? ORDER BY created_at", (root_id, root_id)
    ).fetchall()
    return [M.Task.from_row(r) for r in rows]


def update_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: str | None = None,
    result: dict | None = None,
    plan: dict | None = None,
    slots: dict | None = None,
    scheduled_at: str | None = None,
    clear_schedule: bool = False,
) -> None:
    import json

    current = get_task(conn, task_id)
    if current is None:
        return
    sets, params = [], []
    if status is not None:
        sets.append("status=?")
        params.append(status)
    if result is not None:
        merged = dict(current.result)
        merged.update(result)
        sets.append("result_json=?")
        params.append(json.dumps(merged, ensure_ascii=False))
    if plan is not None:
        merged_plan = dict(current.plan)
        merged_plan.update(plan)
        sets.append("plan_json=?")
        params.append(json.dumps(merged_plan, ensure_ascii=False))
    if slots is not None:
        merged_slots = dict(current.slots)
        merged_slots.update(slots)
        sets.append("slots_json=?")
        params.append(json.dumps(merged_slots, ensure_ascii=False))
    if clear_schedule:
        sets.append("scheduled_at=NULL")
    elif scheduled_at is not None:
        sets.append("scheduled_at=?")
        params.append(scheduled_at)
    sets.append("updated_at=?")
    params.append(now())
    params.append(task_id)
    conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()


def set_task_result_full(conn: sqlite3.Connection, task_id: str, result: dict) -> None:
    """整体替换 result_json（用于核验后定稿）。"""
    import json

    conn.execute(
        "UPDATE tasks SET result_json=?, updated_at=? WHERE id=?",
        (json.dumps(result, ensure_ascii=False), now(), task_id),
    )
    conn.commit()
