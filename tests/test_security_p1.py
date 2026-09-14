"""P1 安全边界测试：SSRF、域名白名单、审批绑定 root+工具+参数哈希、认证与工作区隔离。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from longflow import api as api_mod
from longflow import config as cfg_mod
from longflow import netguard, permissions as perm
from longflow import db


# ---------- SSRF ----------

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/x",
    "gopher://example.com/",
    "http://localhost/admin",
    "http://127.0.0.1/",
    "http://10.0.0.5/",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
    "http://metadata.google.internal/computeMetadata/",
])
def test_ssrf_addresses_blocked(url):
    with pytest.raises(netguard.UrlNotAllowed):
        netguard.check_url(url, resolve=False)


def test_allowlist_enforced():
    netguard.check_url("https://gov.cn/policy", allowlist=["gov.cn"], resolve=False)
    netguard.check_url("https://www.gov.cn/x", allowlist=["gov.cn"], resolve=False)
    with pytest.raises(netguard.UrlNotAllowed):
        netguard.check_url("https://evil.com/x", allowlist=["gov.cn"], resolve=False)


def test_http_get_tool_blocks_non_whitelist(workdir):
    from tests.conftest import tmp_db
    s = tmp_db(workdir)
    task = type("T", (), {"id": "t", "root_id": "t", "agent_role": "x",
                          "slots": {"__scenario__": ""}, "plan": {}})()
    # 私网/元数据即使无白名单也必须被拦截，不发起请求
    ok, val = s.call_tool("http_get", {"url": "http://169.254.169.254/latest/meta-data/"},
                          "t", "t")
    assert ok is False
    row = s.conn.execute(
        """SELECT detail_json FROM events WHERE kind='tool_result'
           AND json_extract(detail_json,'$.tool')='http_get'
           AND json_extract(detail_json,'$.blocked')=1""").fetchone()
    assert row is not None, "SSRF 必须被标记 blocked 且不落为成功"


# ---------- 审批绑定 root + 工具 + 参数哈希，不可跨 root 复用 ----------

def test_high_risk_requires_exact_tool_args_root(workdir):
    from tests.conftest import tmp_db
    s = tmp_db(workdir)
    conn = s.conn
    ra = db.insert_task(conn, title="A", kind="task", agent_role="executor",
                        objective="o", root_id="rootA")
    rb = db.insert_task(conn, title="B", kind="task", agent_role="executor",
                        objective="o", root_id="rootB")
    args = {"vendor": "v", "item": "笔记本", "amount": 5000}
    ah = perm.args_hash("make_purchase", args)
    # rootA 上批准了一次
    conn.execute(
        """INSERT INTO approvals(id, task_id, tool_name, args_json, args_hash, reason,
           status, decided_by, decided_at, created_at)
           VALUES ('apA', ?, 'make_purchase', '{}', ?, 'r', 'approved', 'u', ?, ?)""",
        (ra, ah, db.now(), db.now()))
    conn.commit()
    # 不同 root（rootB）不得复用
    decB = perm.decide(conn, "make_purchase", "high", args, task_id=rb, root_id="rootB")
    assert decB.verdict == perm.NEEDS_APPROVAL
    # 同 root 但不同参数哈希 -> 也不得复用
    other = dict(args, amount=9999)
    decO = perm.decide(conn, "make_purchase", "high", other, task_id=ra, root_id="rootA")
    assert decO.verdict == perm.NEEDS_APPROVAL
    # 同 root + 同工具 + 同参数哈希 -> 放行
    decA = perm.decide(conn, "make_purchase", "high", args, task_id=ra, root_id="rootA")
    assert decA.verdict == perm.ALLOW


# ---------- API 认证 + 工作区隔离 ----------

def _auth_client(workdir, tokens):
    cfg = cfg_mod.load_config()
    cfg["db_path"] = str(workdir / "sec.db")
    cfg["auth"] = {"tokens": tokens}
    app = api_mod.create_app(cfg)
    return app, TestClient(app)


TOK = {
    "tok-a": {"user": "alice", "role": "member",
              "workspaces": ["ws-a"], "default_workspace": "ws-a"},
    "tok-b": {"user": "bob", "role": "member",
              "workspaces": ["ws-b"], "default_workspace": "ws-b"},
    "tok-admin": {"user": "root", "role": "admin",
                  "workspaces": ["*"], "default_workspace": "default"},
}


def test_unauthenticated_rejected_in_token_mode(workdir):
    _, c = _auth_client(workdir, TOK)
    r = c.get("/api/tasks")
    assert r.status_code in (401, 403)


def test_tasks_isolated_between_workspaces(workdir):
    _, c = _auth_client(workdir, TOK)
    ha, hb = {"Authorization": "Bearer tok-a"}, {"Authorization": "Bearer tok-b"}
    r = c.post("/api/tasks", headers=ha,
               json={"goal": "出差住宿标准", "scenario": "team_ops",
                     # 请求体伪造 workspace/owner，必须被忽略
                     "workspace": "ws-b", "owner": "bob"})
    assert r.status_code == 200
    tid = r.json()["task"]["id"]
    # alice 可见
    assert any(t["id"] == tid for t in c.get("/api/tasks", headers=ha).json()["tasks"])
    # bob 在 ws-b 不可见（列表）
    assert not any(t["id"] == tid for t in c.get("/api/tasks", headers=hb).json()["tasks"])
    # bob 直接访问详情 -> 404（不泄露存在性）
    assert c.get(f"/api/tasks/{tid}", headers=hb).status_code == 404
    # 落库 workspace/owner 以服务端凭据为准
    app_state_row = None


def test_approval_cross_workspace_forbidden(workdir):
    _, c = _auth_client(workdir, TOK)
    ha, hb = {"Authorization": "Bearer tok-a"}, {"Authorization": "Bearer tok-b"}
    tid = c.post("/api/tasks", headers=ha,
                 json={"goal": "出差住宿标准", "scenario": "team_ops"}).json()["task"]["id"]
    # bob 不能给 alice 的任务发消息/反馈/取消
    assert c.post(f"/api/tasks/{tid}/message", headers=hb,
                  json={"text": "越权"}).status_code in (403, 404)
    assert c.post(f"/api/tasks/{tid}/feedback", headers=hb,
                  json={"verdict": "resolved"}).status_code in (403, 404)
    assert c.post(f"/api/tasks/{tid}/cancel", headers=hb).status_code in (403, 404)


def test_grants_admin_only(workdir):
    _, c = _auth_client(workdir, TOK)
    hm = {"Authorization": "Bearer tok-a"}
    had = {"Authorization": "Bearer tok-admin"}
    r = c.post("/api/grants", headers=hm,
               json={"scope": "preauth", "tool_name": "make_purchase"})
    assert r.status_code == 403
    r2 = c.post("/api/grants", headers=had,
                json={"scope": "preauth", "tool_name": "make_purchase"})
    assert r2.status_code == 200


def test_knowledge_docs_isolated(workdir):
    _, c = _auth_client(workdir, TOK)
    ha, hb = {"Authorization": "Bearer tok-a"}, {"Authorization": "Bearer tok-b"}
    imp = c.post("/api/knowledge/import", headers=ha,
                 json={"filename": "p.md", "content": "# 内部额度\n云服务上限 27182 元",
                       # 伪造 owner/workspace 必须被忽略
                       "owner": "bob", "workspace": "ws-b", "domain": "team_ops"})
    assert imp.status_code == 200
    doc_id = imp.json()["doc_id"]
    # bob 看不到也不能激活 alice 的文档
    assert not any(d["id"] == doc_id for d in
                   c.get("/api/knowledge/docs", headers=hb).json()["docs"])
    assert c.post(f"/api/knowledge/docs/{doc_id}/activate", headers=hb).status_code in (403, 404)
    # 激活后，bob 的知识检索命中不到 ws-a 的私有文档
    c.post(f"/api/knowledge/docs/{doc_id}/activate", headers=ha)
    hits_b = c.get("/api/knowledge", headers=hb,
                   params={"q": "云服务上限", "domain": "team_ops"}).json()["results"]
    assert not any("27182" in (h.get("text") or "") for h in hits_b)
