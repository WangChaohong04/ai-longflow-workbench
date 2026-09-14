"""问题7：端点级"浏览器主流程"现更名 test_browser_flow_api（TestClient 层，非真实浏览器）。

真实浏览器 DOM 测试见 test_real_server_browser.py（uvicorn 真实服务器 + 可选 playwright）。
HTTP 建任务走与前端完全相同的 /api/tasks（默认 auto_route），引擎推进复用 Engine。
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from longflow import api as api_mod
from longflow import config as cfg_mod
from tests.conftest import tmp_db


def _api(workdir):
    cfg = cfg_mod.load_config()
    cfg["db_path"] = str(workdir / "longflow.db")
    return TestClient(api_mod.create_app(cfg))


def _drive(eng, rid, n=24):
    last = None
    for _ in range(n):
        last = eng.tick(rid)["status"]
        if last in ("completed", "partially_completed", "failed",
                    "waiting_user", "waiting_external", "cancelled"):
            break
    return last


def test_browser_car_goal_only_clarify_all_compare(workdir):
    client = _api(workdir)
    r = client.post("/api/tasks", json={"goal": "10万以内家用车怎么选"})
    assert r.status_code == 200
    tid = r.json()["task"]["id"]
    s = tmp_db(workdir)
    eng = s.engine()
    _drive(eng, tid)
    root = s.task(tid)
    assert root["status"] == "waiting_user"
    assert (root["slots"] or {}).get("__domain__") == "car_research"
    client.post(f"/api/tasks/{tid}/message", json={"text": "全部比较"})
    _drive(eng, tid)
    root2 = s.task(tid)
    assert root2["status"] == "partially_completed"
    res = root2["result"]
    children = [c for c in s.tasks_of_root(tid) if c["id"] != tid]
    branches = {json.loads(c["plan_json"]).get("branch")
                for c in children if json.loads(c["plan_json"]).get("branch")}
    assert {"icev", "bev", "phev", "erev"} <= branches
    assert res["verified"] is False
    bucket = res.get("verification_bucket") or res.get("status_bucket")
    assert bucket in ("partial", "insufficient", "failed")


def test_browser_high_risk_pauses_without_executing(workdir):
    client = _api(workdir)
    r = client.post("/api/tasks", json={"goal": "帮我下单买10台笔记本 预算5万"})
    tid = r.json()["task"]["id"]
    s = tmp_db(workdir)
    eng = s.engine()
    _drive(eng, tid)
    root = s.task(tid)
    assert root["status"] in ("waiting_user", "waiting_external")
    children = [c for c in s.tasks_of_root(tid) if c["id"] != tid]
    assert children == []