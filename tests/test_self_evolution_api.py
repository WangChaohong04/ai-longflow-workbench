"""P7：任务结束记轨迹；候选改进只登记 proposed；评测/审核/灰度/回滚需管理员。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from longflow import api as api_mod, config as cfg_mod, improvements as impr
from longflow import domains as dom_mod
from tests.conftest import tmp_db

TOK = {
    "member": {"user": "bob", "role": "member", "workspaces": ["default"],
               "default_workspace": "default"},
    "admin": {"user": "root", "role": "admin", "workspaces": ["*"],
              "default_workspace": "default"},
}


def _client(workdir):
    cfg = cfg_mod.load_config()
    cfg["db_path"] = str(workdir / "ev.db")
    cfg["auth"] = {"tokens": TOK}
    return TestClient(api_mod.create_app(cfg))


def _drive(eng, rid, n=12):
    last = None
    for _ in range(n):
        last = eng.tick(rid)["status"]
        if last in ("completed", "partially_completed", "failed", "waiting_user"):
            break
    return last


def test_run_telemetry_recorded_after_domain_task(workdir):
    s = tmp_db(workdir)
    eng = s.engine()
    reg = dom_mod.load_default_registry()
    r = eng.create_domain_goal("出差住宿标准是多少", registry=reg)
    _drive(eng, r["task_id"])
    rows = s.conn.execute("SELECT * FROM run_telemetry").fetchall()
    assert len(rows) >= 1
    row = rows[-1]
    assert row["goal"]
    assert row["subagents_json"]  # 记录了 subagent
    assert "coordinator" in (row["route_json"] or "")


def test_suggest_only_creates_proposed(workdir):
    client = _client(workdir)
    had = {"Authorization": "Bearer admin"}
    r = client.post("/api/improvements/suggest", headers=had,
                    json={"attributions": ["rag_no_recall", "tool_failure"]})
    assert r.status_code == 200
    ids = r.json()["created"]
    assert len(ids) == 2 and r.json()["status"] == "proposed"
    listed = client.get("/api/improvements?status=proposed", headers=had).json()["improvements"]
    got = {x["id"]: x for x in listed}
    for i in ids:
        assert got[i]["status"] == "proposed"
        # 候选 payload 绝不等于已应用（无任何写 prompt/权限动作）
    # rollout 未 approved 必须被拒
    blocked = client.post(f"/api/improvements/{ids[0]}/rollout", headers=had)
    assert blocked.status_code == 409


def test_full_lifecycle_requires_admin(workdir):
    client = _client(workdir)
    hm, had = {"Authorization": "Bearer member"}, {"Authorization": "Bearer admin"}
    # 普通成员不能生成候选/评测/批准/灰度/回滚/查看 runs
    assert client.post("/api/improvements/suggest", headers=hm,
                       json={"attributions": ["slot_extraction_error"]}).status_code == 403
    assert client.get("/api/runs", headers=hm).status_code == 403
    # 管理员走完整生命周期
    iid = client.post("/api/improvements/suggest", headers=had,
                      json={"attributions": ["slot_extraction_error"]}).json()["created"][0]
    assert client.post(f"/api/improvements/{iid}/evaluate", headers=had,
                       json={"eval_result": {"regression": "pass", "delta": "+2%"}}).status_code == 200
    assert client.post(f"/api/improvements/{iid}/approve", headers=had).status_code == 200
    assert client.post(f"/api/improvements/{iid}/rollout", headers=had).status_code == 200
    final = {x["id"]: x for x in client.get("/api/improvements", headers=had).json()["improvements"]}
    assert final[iid]["status"] == "rolled_out"
    assert client.post(f"/api/improvements/{iid}/rollback", headers=had).status_code == 200
    final2 = {x["id"]: x for x in client.get("/api/improvements", headers=had).json()["improvements"]}
    assert final2[iid]["status"] == "rolled_back"


def test_cannot_rollout_without_approval_directly(workdir):
    s = tmp_db(workdir)
    iid = impr.propose_improvement(
        s.conn, kind="prompt", target="router", rationale="t",
        attribution="routing_error", payload={"x": 1})
    import pytest
    with pytest.raises(PermissionError):
        impr.roll_out(s.conn, iid, by="root")  # 未 evaluate/approve
