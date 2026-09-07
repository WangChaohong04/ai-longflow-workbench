"""审计事件（只增日志）。记录动作与事实，不记录模型内部思维链。"""
from __future__ import annotations

import json
import sqlite3

from . import db
from .redaction import redact_obj

# 事件类型
TASK_CREATED = "task_created"
TASK_STATUS = "task_status"
TOOL_REQUEST = "tool_request"
TOOL_RESULT = "tool_result"
TOOL_DENIED = "tool_denied"
APPROVAL_REQUESTED = "approval_requested"
APPROVAL_DECIDED = "approval_decided"
GATE_ENTRY = "gate_entry"
GATE_EXIT = "gate_exit"
MESSAGE = "message"
LLM_CALL = "llm_call"
ERROR = "error"
RECOVERY = "recovery"
CLARIFY = "clarify"


def emit(
    conn: sqlite3.Connection,
    kind: str,
    *,
    task_id: str | None = None,
    actor: str | None = None,
    detail: dict | None = None,
) -> int:
    safe_detail = redact_obj(detail or {})
    cur = conn.execute(
        "INSERT INTO events (ts, task_id, kind, actor, detail_json) VALUES (?,?,?,?,?)",
        (db.now(), task_id, kind, actor, json.dumps(safe_detail, ensure_ascii=False)),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_events(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    """返回某任务子树（同 root）的全部事件，按时间升序。"""
    rows = conn.execute(
        """SELECT e.* FROM events e
           JOIN tasks t ON t.id = COALESCE(e.task_id, ?)
           WHERE t.root_id = (SELECT root_id FROM tasks WHERE id=?)
           ORDER BY e.id""",
        (task_id, task_id),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "ts": r["ts"],
            "task_id": r["task_id"],
            "kind": r["kind"],
            "actor": r["actor"],
            "detail": json.loads(r["detail_json"] or "{}"),
        }
        for r in rows
    ]


def find_side_effect_result(
    conn: sqlite3.Connection, idempotency_key: str
) -> dict | None:
    """按幂等键查找已成功的副作用工具结果（用于重启恢复，不重复执行）。"""
    rows = conn.execute(
        "SELECT detail_json FROM events WHERE kind=? ORDER BY id", (TOOL_RESULT,)
    ).fetchall()
    for r in rows:
        detail = json.loads(r["detail_json"] or "{}")
        if detail.get("idempotency_key") == idempotency_key and detail.get("ok"):
            return detail.get("result")
    return None
