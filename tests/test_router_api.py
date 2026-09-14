"""批次6：总路由/领域/subagent/文件导入的 HTTP API 闭环。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from longflow import api as api_mod, config as cfg_mod, rag


def _client(workdir):
    cfg = cfg_mod.load_config()
    cfg["db_path"] = str(workdir / "api.db")
    app = api_mod.create_app(cfg)
    return app, TestClient(app)


def test_route_endpoint_classifies(workdir):
    _, client = _client(workdir)
    r = client.post("/api/route", json={"text": "出差住宿标准是多少？"})
    assert r.status_code == 200
    d = r.json()
    assert d["mode"] == "activate_domain"
    assert d["domains"][0]["id"] == "team_ops"

    r2 = client.post("/api/route", json={"text": "我要采购笔记本 预算8000元"})
    d2 = r2.json()
    assert d2["risk"] == "high" and d2["needs_user"] is True


def test_domains_and_subagents_listed(workdir):
    _, client = _client(workdir)
    dom = client.get("/api/domains").json()
    ids = {d["id"] for d in dom["domains"]}
    assert {"team_ops", "geo_site"} <= ids
    sa = client.get("/api/subagents").json()["subagents"]
    sa_ids = {s["id"] for s in sa}
    assert {"rag_researcher", "geo_researcher", "evidence_verifier", "comparison_agent"} <= sa_ids
    # 固定 subagent 一律不产出最终决策
    assert all(s["returns_decision"] is False for s in sa)


def test_import_preview_then_activate_flow(workdir):
    _, client = _client(workdir)
    # 1) 导入 -> 预览（不参与检索）
    r = client.post("/api/knowledge/import", json={
        "filename": "spec.md", "content": "# 专属规范\n版本 v9\n## 额度\n特殊设备上限 31415 元",
        "workspace": "wsx", "domain": "team_ops"})
    assert r.status_code == 200
    doc = r.json()
    assert doc["status"] == "preview"
    # 预览时知识检索不到
    k0 = client.get("/api/knowledge", params={"q": "特殊设备上限 31415",
                                              "scenario": "user:wsx:team_ops"}).json()
    assert all("31415" not in (c.get("text") or "") for c in k0["results"])
    # 2) 用户确认激活
    a = client.post(f"/api/knowledge/docs/{doc['doc_id']}/activate", json={"by": "tester"})
    assert a.status_code == 200 and a.json()["chunks"] >= 1
    # 激活后可检索
    k1 = client.get("/api/knowledge", params={"q": "特殊设备上限",
                                              "scenario": "user:wsx:team_ops"}).json()
    assert any("31415" in (c.get("text") or "") for c in k1["results"])


def test_import_bad_extension_422(workdir):
    _, client = _client(workdir)
    r = client.post("/api/knowledge/import", json={
        "filename": "x.exe", "content": "bad", "workspace": "w", "domain": "d"})
    assert r.status_code == 422


def test_subagent_run_rag_returns_evidence(workdir):
    _, client = _client(workdir)
    # 装载 team_ops 知识（create_app 默认加载）；直接跑 rag_researcher
    r = client.post("/api/subagents/run", json={
        "subagent": "rag_researcher", "query": "出差住宿标准", "scenario": "team_ops"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["subagent"] == "rag_researcher"
    assert d["evidence"] and "source_type" in d["evidence"][0]
    # 固定 subagent 不返回决策
    assert "decision" not in d


def test_subagent_run_unknown_422(workdir):
    _, client = _client(workdir)
    r = client.post("/api/subagents/run", json={"subagent": "ghost", "query": "x"})
    assert r.status_code == 422


def test_subagent_run_web_unconfigured(workdir):
    _, client = _client(workdir)
    r = client.post("/api/subagents/run", json={"subagent": "web_researcher", "query": "x"})
    d = r.json()
    assert d["ok"] is False and d["needs_user"] is False
    assert d["error"] == "source_not_configured"
