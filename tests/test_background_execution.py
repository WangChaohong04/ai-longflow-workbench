"""问题5：建任务后台执行（不阻塞响应）+ 首次调度失败记录 + worker 有界并发。"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from longflow import api as api_mod
from longflow import config as cfg_mod
from longflow import db, events
from longflow.workers import RecoveryWorker


def _make_app(workdir):
    cfg = cfg_mod.load_config()
    cfg["db_path"] = str(workdir / "q5.db")
    cfg["limits"]["worker_concurrency"] = 4
    return cfg, api_mod.create_app(cfg)


def test_create_task_delegates_execution_to_worker(workdir):
    # 不用 context manager -> 无 worker；创建只物化不执行慢节点
    cfg, app = _make_app(workdir)
    c = TestClient(app)
    t0 = time.monotonic()
    r = c.post("/api/tasks", json={"goal": "出差住宿标准",
                                    "scenario": "team_ops", "slots": {}})
    assert r.status_code == 200
    assert time.monotonic() - t0 < 2.0  # 不阻塞在慢执行上
    tid = r.json()["task"]["id"]
    conn = db.connect(cfg["db_path"])
    children = [dict(x) for x in
                conn.execute("SELECT * FROM tasks WHERE root_id=? AND id<>?",
                             (tid, tid)).fetchall()]
    # 创建阶段不执行研究节点：子任务都未被跑到 completed
    assert children, "应已物化子节点"
    assert all(x["status"] != "completed" for x in children), "慢节点不应在创建响应内执行"
    conn.close()


def test_scheduling_failure_recorded(workdir, monkeypatch):
    cfg, app = _make_app(workdir)
    from longflow.orchestrator import Engine
    def boom(*a, **k):
        raise RuntimeError("db write failed")
    monkeypatch.setattr(Engine, "create_domain_goal", boom)
    c = TestClient(app)
    r = c.post("/api/tasks", json={"goal": "10万以内家用车"})
    assert r.status_code == 500
    body = r.json().get("detail", r.json())
    assert body.get("code") == "create_failed"
    # 失败原因被记录（不静默吞掉）
    conn = db.connect(cfg["db_path"])
    evs = conn.execute("SELECT * FROM events WHERE detail_json LIKE '%scheduling_failed%'").fetchall()
    assert evs
    conn.close()


def test_worker_parallel_roots_overlap_and_atomic(workdir):
    state = {"cur": 0, "peak": 0, "done": 0}
    state["lock"] = threading.Lock()
    ids = ["r1", "r2"]

    class _CC:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _FakeEngine:
        def __init__(self):
            self.conn = self

        def execute(self, sql, *a):
            return _CC([{"root_id": i} for i in ids])

        def tick(self, root_id):
            with state["lock"]:
                state["cur"] += 1
                state["peak"] = max(state["peak"], state["cur"])
            time.sleep(0.05)
            with state["lock"]:
                state["cur"] -= 1
                state["done"] += 1

        def close(self):
            pass

    w = RecoveryWorker(lambda: _FakeEngine(), interval_seconds=999, max_parallel=4)
    w.start()
    time.sleep(0.4)
    w.stop()
    # 两个独立根并发重叠执行（不只串行逐个），且都完成
    assert state["peak"] >= 2, f"独立根应并发，peak={state['peak']}"
    assert state["done"] == 2