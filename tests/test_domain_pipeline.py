"""P2/P3 端到端：默认建任务经 Router（不要求 scenario）、汽车澄清→"全部比较"四分支、
部分失败 partially_completed、高风险不自动执行。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from longflow import api as api_mod
from longflow import config as cfg_mod
from longflow import domains as dom_mod
from longflow.models import (WAITING_USER, PARTIALLY_COMPLETED, COMPLETED,
                             WAITING_EXTERNAL)
from tests.conftest import tmp_db


def _drive(eng, rid, n=12):
    last = None
    for _ in range(n):
        last = eng.tick(rid)["status"]
        if last in ("completed", "partially_completed", "failed", "cancelled",
                    "waiting_user", "waiting_external"):
            break
    return last


def _api(workdir):
    cfg = cfg_mod.load_config()
    cfg["db_path"] = str(workdir / "pipe.db")
    app = api_mod.create_app(cfg)
    return TestClient(app)


def test_default_task_uses_router_without_scenario(workdir):
    client = _api(workdir)
    # 不传 scenario：默认走 Router（团队行政知识问答，可直接完成）
    r = client.post("/api/tasks", json={"goal": "出差住宿标准是多少？"})
    assert r.status_code == 200
    d = r.json()
    root = d["task"]
    # 自动识别领域，不要求手选场景
    assert (root["plan"] or {}).get("engine") == "coordinator"
    assert (root["plan"] or {}).get("domain") == "team_ops"


def test_car_missing_energy_then_all_compare_four_branches(workdir):
    s = tmp_db(workdir)
    eng = s.engine()
    reg = dom_mod.load_default_registry()
    r = eng.create_domain_goal("10万以内家用车推荐", registry=reg)
    rid = r["task_id"]
    # 缺能源类型 -> waiting_user
    assert r["verdict"] == "clarify"
    assert s.task(rid)["status"] == WAITING_USER
    assert any(m["name"] == "energy_type" for m in r["missing_slots"])
    # 用户回答"全部比较"
    eng.user_message(rid, "全部比较")
    final = _drive(eng, rid)
    children = [c for c in s.tasks_of_root(rid) if c["id"] != rid]
    # 四个研究分支都出现（节点 branch 标签）
    branches = {(c["plan_json"] and __import__("json").loads(c["plan_json"]).get("branch"))
                for c in children}
    assert {"icev", "bev", "phev", "erev"} <= {b for b in branches if b}
    # 汇聚节点存在
    subs = {__import__("json").loads(c["plan_json"]).get("subagent") for c in children}
    assert {"normalizer", "evidence_verifier", "comparison_agent"} <= subs
    # 外部网页来源未配置 -> 部分来源失败 -> partially_completed（不伪装成功）
    assert final == PARTIALLY_COMPLETED
    res = s.task(rid)["result"]
    assert res["verified"] is False
    assert set(res["branches_failed"]) == set() or isinstance(res["failures"], list)


def test_high_risk_does_not_auto_execute(workdir):
    s = tmp_db(workdir)
    eng = s.engine()
    reg = dom_mod.load_default_registry()
    r = eng.create_domain_goal("帮我下单买10台笔记本 预算5万", registry=reg)
    rid = r["task_id"]
    # 高风险：不允许自动物化任务图，进入等待用户确认
    assert r["verdict"] in ("await_confirmation", "clarify")
    root = s.task(rid)
    assert root["status"] in (WAITING_USER, WAITING_EXTERNAL)
    # 未产生任何子任务（未自动执行）
    children = [c for c in s.tasks_of_root(rid) if c["id"] != rid]
    assert children == []


def test_single_energy_completes_one_branch(workdir):
    s = tmp_db(workdir)
    eng = s.engine()
    reg = dom_mod.load_default_registry()
    r = eng.create_domain_goal("10万以内家用车", registry=reg)
    rid = r["task_id"]
    eng.user_message(rid, "纯电")
    _drive(eng, rid)
    children = [c for c in s.tasks_of_root(rid) if c["id"] != rid]
    branches = {__import__("json").loads(c["plan_json"]).get("branch")
                for c in children if __import__("json").loads(c["plan_json"]).get("branch")}
    assert branches == {"bev"}


def test_legacy_scenario_path_still_works(workdir):
    # 显式 scenario 走旧静态模板路径（兼容）
    client = _api(workdir)
    r = client.post("/api/tasks", json={"goal": "出差住宿标准", "scenario": "team_ops"})
    assert r.status_code == 200
    root = r.json()["task"]
    # 旧路径不打 coordinator 引擎标记
    assert (root["plan"] or {}).get("engine") != "coordinator"
