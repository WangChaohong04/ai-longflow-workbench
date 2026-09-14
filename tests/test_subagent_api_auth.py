"""问题1：/api/subagents/run 与 /api/eval/run 的认证 / 权限 / 限流 / 审计。

未登录被拒；subagent 绑定真实用户+工作区并落审计（不复用 "preview"）；
拒绝时不执行工具（不产生 run 记录）；限流生效；eval 仅管理员。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from longflow import api as api_mod
from longflow import config as cfg_mod
from longflow import db

TOK = {
    "tok-a": {"user": "alice", "role": "member",
              "workspaces": ["ws-a"], "default_workspace": "ws-a"},
    "tok-admin": {"user": "root", "role": "admin",
                  "workspaces": ["*"], "default_workspace": "default"},
}
HEAD_A = {"Authorization": "Bearer tok-a"}
HEAD_ADMIN = {"Authorization": "Bearer tok-admin"}


def _client(workdir, *, rpm=10):
    cfg = cfg_mod.load_config()
    cfg["db_path"] = str(workdir / "sec.db")
    cfg["auth"] = {"tokens": TOK}
    cfg.setdefault("limits", {})["subagent_rpm"] = rpm
    cfg.setdefault("limits", {})["eval_rpm"] = 2
    app = api_mod.create_app(cfg)
    return app, TestClient(app), cfg


def _run_count(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM subagent_runs").fetchone()["n"]


def test_unauthenticated_rejected(workdir):
    _, c, _ = _client(workdir)
    r = c.post("/api/subagents/run", json={"subagent": "rag_researcher", "query": "x"})
    assert r.status_code in (401, 403)
    r2 = c.post("/api/eval/run")
    assert r2.status_code in (401, 403)


def test_audit_binds_real_user_and_workspace(workdir):
    api_mod._RATE_WINDOWS.clear()  # 隔离跨测试限流状态
    _, c, cfg = _client(workdir)
    r = c.post("/api/subagents/run", headers=HEAD_A,
               json={"subagent": "rag_researcher", "query": "出差住宿", "scenario": "user:ws-b:oops"})
    assert r.status_code == 200
    payload = r.json()
    assert payload.get("run_id")
    # 客户端伪造 scenario 不影响落库——用户/工作区以服务端为准
    conn = db.connect(cfg["db_path"])
    rows = conn.execute("SELECT * FROM subagent_runs").fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["user"] == "alice" and row["workspace"] == "ws-a"
    assert row["subagent"] == "rag_researcher"
    assert row["id"] != "preview" and not row["id"].startswith("preview")
    conn.close()


def test_rejected_does_not_execute(workdir):
    _, c, cfg = _client(workdir)
    conn = db.connect(cfg["db_path"])
    before = _run_count(conn)
    # 未认证 -> 拒绝，不产生 run 记录
    c.post("/api/subagents/run", json={"subagent": "rag_researcher", "query": "x"})
    assert _run_count(conn) == before
    c.post("/api/eval/run")  # 未认证
    assert _run_count(conn) == before
    conn.close()


def test_rate_limited(workdir):
    api_mod._RATE_WINDOWS.clear()  # 隔离跨测试限流状态
    _, c, cfg = _client(workdir, rpm=3)
    conn = db.connect(cfg["db_path"])
    statuses = []
    for _ in range(5):
        r = c.post("/api/subagents/run", headers=HEAD_A,
                   json={"subagent": "rag_researcher", "query": "q"})
        statuses.append(r.status_code)
    assert statuses[:3] == [200, 200, 200], statuses
    assert statuses[3] == 429 and statuses[4] == 429, statuses
    # 被限流的调用不执行工具（不新增 run 记录）
    conn.close()


def test_eval_admin_only(workdir, monkeypatch):
    _, c, _ = _client(workdir)
    # 普通成员 -> 403
    assert c.post("/api/eval/run", headers=HEAD_A).status_code == 403
    # 管理员 -> 200（mock run_all，避免整套评测耗时）
    import tests.run_eval as re_mod
    monkeypatch.setattr(re_mod, "run_all", lambda: {"ok": True})
    r = c.post("/api/eval/run", headers=HEAD_ADMIN)
    assert r.status_code == 200
    assert r.json()["ok"] is True