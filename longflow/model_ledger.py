"""模型调用账本：真实模型调用的可观察性与预算。

只记录"真实跨模型边界的调用"（plan / next_action / draft_answer / extract_slots）：
driver、model、调用类型、耗时、token（prompt/completion，未知则 None 不写 0）、
成功/失败、是否降级到本地规则。模板动作与 LocalDriver 的确定性规则不经模型，
不记为模型调用（由编排层另发 template_action 事件）。
"""
from __future__ import annotations

import time

from . import db

# 调用类型
PLAN = "plan"
NEXT_ACTION = "next_action"
DRAFT = "draft_answer"
EXTRACT = "extract_slots"


def init_ledger(conn) -> None:
    """幂等建表（随 init_db 之外的增强迁移）。"""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS llm_calls (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             ts TEXT NOT NULL,
             task_id TEXT,
             root_id TEXT,
             driver TEXT,
             model TEXT,
             call_type TEXT,
             ok INTEGER,
             fallback INTEGER DEFAULT 0,
             latency_ms INTEGER,
             prompt_tokens INTEGER,
             completion_tokens INTEGER,
             error TEXT
           )"""
    )
    conn.commit()


def record_call(
    conn,
    *,
    driver: str,
    model: str | None,
    call_type: str,
    ok: bool,
    fallback: bool = False,
    latency_ms: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    error: str | None = None,
    task_id: str | None = None,
    root_id: str | None = None,
) -> int:
    init_ledger(conn)
    cur = conn.execute(
        """INSERT INTO llm_calls
           (ts, task_id, root_id, driver, model, call_type, ok, fallback,
            latency_ms, prompt_tokens, completion_tokens, error)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (db.now(), task_id, root_id, driver, model, call_type, 1 if ok else 0,
         1 if fallback else 0, latency_ms, prompt_tokens, completion_tokens,
         (error or None) and str(error)[:300]),
    )
    conn.commit()
    return int(cur.lastrowid)


def count_calls(conn, root_id: str | None = None) -> int:
    init_ledger(conn)
    if root_id:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM llm_calls WHERE root_id=?", (root_id,)
        ).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS n FROM llm_calls").fetchone()
    return int(row["n"])


class ModelBudget:
    """调用预算：超过 max_calls 时拒绝再发起真实模型调用（可恢复错误在预算内重试）。"""

    def __init__(self, conn, max_calls: int | None = None):
        self.conn = conn
        self.max_calls = max_calls

    def remaining(self, root_id: str | None = None) -> int | None:
        if self.max_calls is None:
            return None
        return max(0, self.max_calls - count_calls(self.conn, root_id))

    def check(self, root_id: str | None = None) -> None:
        if self.max_calls is None:
            return
        if count_calls(self.conn, root_id) >= self.max_calls:
            raise RuntimeError(f"模型调用预算已用尽（上限 {self.max_calls} 次），停止发起新的模型调用")


class TimedCall:
    """上下文管理器：计时 + 记录一次模型调用，token 未知保持 None。"""

    def __init__(self, conn, *, driver: str, model: str | None, call_type: str,
                 task_id: str | None = None, root_id: str | None = None,
                 budget: ModelBudget | None = None):
        self.conn = conn
        self.driver = driver
        self.model = model
        self.call_type = call_type
        self.task_id = task_id
        self.root_id = root_id
        self.budget = budget
        self.fallback = False
        self.error: str | None = None
        self.latency_ms: int | None = None
        self.prompt_tokens: int | None = None
        self.completion_tokens: int | None = None
        self._t0 = 0.0

    def __enter__(self):
        if self.budget is not None:
            self.budget.check(self.root_id)
        self._t0 = time.monotonic()
        return self

    def set_usage(self, usage: dict | None):
        if not isinstance(usage, dict):
            return
        def _i(*keys):
            for k in keys:
                v = usage.get(k)
                if isinstance(v, int) and v >= 0:
                    return v
            return None
        self.prompt_tokens = _i("prompt_tokens", "input_tokens")
        self.completion_tokens = _i("completion_tokens", "output_tokens")

    def mark_fallback(self):
        self.fallback = True

    def fail(self, exc: Exception):
        self.error = str(exc)[:300]

    def __exit__(self, exc_type, exc, tb):
        self.latency_ms = int((time.monotonic() - self._t0) * 1000)
        ok = exc_type is None
        if exc is not None and self.error is None:
            self.error = str(exc)[:300]
        record_call(
            self.conn, driver=self.driver, model=self.model, call_type=self.call_type,
            ok=ok, fallback=self.fallback, latency_ms=self.latency_ms,
            prompt_tokens=self.prompt_tokens, completion_tokens=self.completion_tokens,
            error=self.error, task_id=self.task_id, root_id=self.root_id,
        )
        return False  # 不吞异常
