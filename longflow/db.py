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



CREATE TABLE IF NOT EXISTS run_telemetry (
  id TEXT PRIMARY KEY,
  root_id TEXT,
  goal TEXT,
  final_status TEXT,
  route_json TEXT,
  domains_json TEXT,
  subagents_json TEXT,
  sources_json TEXT,
  evidence_count INTEGER DEFAULT 0,
  tool_calls_json TEXT,
  tool_failures_json TEXT,
  user_outcome TEXT,
  feedback TEXT,
  latency_ms INTEGER,
  llm_calls INTEGER DEFAULT 0,
  decision_ready INTEGER DEFAULT 0,
  attributions_json TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_root ON run_telemetry(root_id);

CREATE TABLE IF NOT EXISTS improvements (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,           -- prompt|slot_rule|retrieval|knowledge|tool|subagent
  target TEXT,                  -- 被改进对象
  rationale TEXT,               -- 归因依据
  attribution TEXT,             -- 失败归因码
  payload_json TEXT,            -- 候选内容（不落生产）
  status TEXT NOT NULL DEFAULT 'proposed',  -- proposed|evaluated|approved|rejected|rolled_out|rolled_back
  eval_result_json TEXT,
  scope TEXT,
  rollback TEXT,
  version TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT,
  decided_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_improvements_status ON improvements(status);

CREATE TABLE IF NOT EXISTS feedback (
  id TEXT PRIMARY KEY,
  root_id TEXT,
  task_id TEXT,
  verdict TEXT NOT NULL,
  category TEXT,
  comment TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_root ON feedback(root_id);

CREATE TABLE IF NOT EXISTS eval_runs (
  id TEXT PRIMARY KEY,
  ts TEXT,
  case_id TEXT,
  passed INTEGER,
  checks_json TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS subagent_runs (
  id TEXT PRIMARY KEY,
  user TEXT NOT NULL,
  workspace TEXT NOT NULL,
  subagent TEXT NOT NULL,
  query TEXT,
  requested_at TEXT NOT NULL,
  duration_ms INTEGER,
  ok INTEGER,
  source TEXT,
  error TEXT,
  result_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_subagent_runs_ws_ts ON subagent_runs(workspace, requested_at);
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


def _ensure_col(conn, table: str, col: str, decl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db(conn) -> sqlite3.Connection:
    """初始化数据库。接受已建连接，或数据库文件路径（返回连接）。"""
    if isinstance(conn, str):
        conn = connect(conn)
    conn.executescript(SCHEMA)
    # 工作区/属主隔离列（幂等迁移；旧数据默认归 default/local）
    _ensure_col(conn, "tasks", "workspace", "TEXT NOT NULL DEFAULT 'default'")
    _ensure_col(conn, "tasks", "owner", "TEXT NOT NULL DEFAULT 'local'")
    _ensure_col(conn, "approvals", "workspace", "TEXT NOT NULL DEFAULT 'default'")
    _ensure_col(conn, "grants", "workspace", "TEXT NOT NULL DEFAULT 'default'")
    _ensure_col(conn, "feedback", "workspace", "TEXT NOT NULL DEFAULT 'default'")
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
    workspace: str = "default",
    owner: str = "local",
) -> str:
    import json

    tid = new_id("t")
    ts = now()
    conn.execute(
        """INSERT INTO tasks (id, parent_id, root_id, title, kind, agent_role, status,
           objective, slots_json, plan_json, result_json, depends_on_json, created_at, updated_at,
           workspace, owner)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
            workspace,
            owner,
        ),
    )
    conn.commit()
    return tid


# ---- Subagent 预览/评测运行审计记录 ----

def record_subagent_run(conn, *, user, workspace, subagent, query,
                        ok, source="", error="", result_summary="", duration_ms=None):
    """记录一次 subagent 运行（认证用户 + 工作区 + 可审计）。返回 run id。

    每次真实调用生成独立 run id，绝不共用固定的 "preview" 标识。
    """
    run_id = new_id("run")
    conn.execute(
        """INSERT INTO subagent_runs
           (id, user, workspace, subagent, query, requested_at, duration_ms, ok,
            source, error, result_summary)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, user, workspace, subagent, query, now(), duration_ms,
         1 if ok else 0, source, (error or "")[:500], (result_summary or "")[:2000]),
    )
    conn.commit()
    return run_id


def list_subagent_runs(conn, *, workspace=None, user=None, limit=50):
    q = "SELECT * FROM subagent_runs WHERE 1=1"
    args = []
    if workspace:
        q += " AND workspace=?"; args.append(workspace)
    if user:
        q += " AND user=?"; args.append(user)
    q += " ORDER BY requested_at DESC LIMIT " + str(int(limit))
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def list_roots(conn, *, workspace: str | None = None) -> list[M.Task]:
    sql = "SELECT * FROM tasks WHERE parent_id IS NULL"
    args: list = []
    if workspace is not None:
        sql += " AND workspace=?"; args.append(workspace)
    sql += " ORDER BY created_at"
    return [M.Task.from_row(r) for r in conn.execute(sql, args).fetchall()]


def get_task_for(conn, task_id: str, *, workspace: str | None = None,
                 owner: str | None = None, is_admin: bool = False) -> M.Task | None:
    """按工作区/属主取任务；工作区不匹配返回 None（不泄露存在性细节）。"""
    task = get_task(conn, task_id)
    if task is None:
        return None
    root = task if task.parent_id is None else get_task(conn, task.root_id)
    row = conn.execute("SELECT workspace, owner FROM tasks WHERE id=?", (task.root_id,)).fetchone()
    if workspace is not None and not is_admin and row["workspace"] != workspace:
        return None
    if owner is not None and not is_admin and row["owner"] != owner:
        return None
    return task


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
    expected_statuses: tuple[str, ...] | None = None,
    require_active_root: bool = False,
) -> bool:
    import json

    current = get_task(conn, task_id)
    if current is None:
        return False
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
    guard = ""
    if expected_statuses is not None:
        guard += " AND status IN (" + ",".join("?" for _ in expected_statuses) + ")"
        params.extend(expected_statuses)
    if require_active_root:
        terminal = tuple(M.TERMINAL_STATUSES)
        guard += (" AND EXISTS (SELECT 1 FROM tasks root WHERE root.id=tasks.root_id"
                  " AND root.status NOT IN (" + ",".join("?" for _ in terminal) + "))")
        params.extend(terminal)
    cursor = conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?{guard}", params)
    conn.commit()
    return cursor.rowcount == 1


def set_task_result_full(conn: sqlite3.Connection, task_id: str, result: dict) -> None:
    """整体替换 result_json（用于核验后定稿）。"""
    import json

    conn.execute(
        "UPDATE tasks SET result_json=?, updated_at=? WHERE id=?",
        (json.dumps(result, ensure_ascii=False), now(), task_id),
    )
    conn.commit()


def transition_status(
    conn: sqlite3.Connection,
    task_id: str,
    expected: tuple[str, ...] | list[str],
    new_status: str,
) -> bool:
    """原子状态 CAS：仅当当前状态在 expected 中时才更新为 new_status。

    用于后台 worker 领取任务（READY/PENDING -> IN_PROGRESS），避免同一任务被
    多个 worker/请求同时领取。返回是否抢到。
    """
    placeholders = ",".join("?" * len(expected))
    cur = conn.execute(
        f"UPDATE tasks SET status=?, updated_at=? WHERE id=? AND status IN ({placeholders})",
        (new_status, now(), task_id, *expected),
    )
    conn.commit()
    return cur.rowcount == 1


def is_status(conn: sqlite3.Connection, task_id: str, *statuses: str) -> bool:
    """任务当前是否处于给定状态之一（用于迟到结果/取消守卫）。"""
    if not statuses:
        return False
    placeholders = ",".join("?" * len(statuses))
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM tasks WHERE id=? AND status IN ({placeholders})",
        (task_id, *statuses),
    ).fetchone()
    return bool(row and row["n"])


def reset_stale_inprogress(conn, stale_seconds: float = 120.0) -> int:
    """回收崩溃遗留的 IN_PROGRESS 任务：超过 stale_seconds 未更新者重置为 READY。

    进程重启恢复：崩溃时停在 in_progress 的任务可能从未完成，需重跑；
    正在运行的任务 updated_at 较新，不会被误回收，避免并发重复执行。
    副作用工具超时置 waiting_event（in_doubt），不在回收范围。
    """
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat(timespec="seconds")
    cur = conn.execute(
        "UPDATE tasks SET status='ready', updated_at=? WHERE status='in_progress' AND updated_at < ?",
        (now(), cutoff),
    )
    conn.commit()
    return cur.rowcount


def list_recoverable_roots(conn: sqlite3.Connection) -> list[str]:
    """返回需要后台推进的 root_id：根任务未终结，且自身或子任务仍在非终态。"""
    rows = conn.execute(
        """SELECT DISTINCT root_id FROM tasks
           WHERE status NOT IN ('completed','failed','cancelled')"""
    ).fetchall()
    return [r["root_id"] for r in rows]


def add_feedback(conn, *, root_id, task_id, verdict, category=None, comment=None) -> str:
    """记录一条用户结果反馈。verdict: resolved|unresolved|answer_wrong；
    category: retrieval|generation|tool|routing|ui|other。仅记录，不自动写知识库。"""
    fid = new_id("fb")
    conn.execute(
        """INSERT INTO feedback (id, root_id, task_id, verdict, category, comment, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (fid, root_id, task_id, verdict, category, comment, now()),
    )
    conn.commit()
    return fid


def list_feedback(conn, root_id: str | None = None) -> list[dict]:
    if root_id:
        rows = conn.execute(
            "SELECT * FROM feedback WHERE root_id=? ORDER BY id", (root_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM feedback ORDER BY id").fetchall()
    return [dict(r) for r in rows]
