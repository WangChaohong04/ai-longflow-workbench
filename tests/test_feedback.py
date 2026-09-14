"""Batch7：结果反馈机制——记录已解决/未解决/答案有误 + 归因分类；未复核不写知识库。"""
from tests.conftest import workdir  # noqa: F401

from fastapi.testclient import TestClient

from longflow import api as api_mod, config as cfg_mod, db, rag


def _client(workdir):
    cfg = cfg_mod.load_config()
    cfg["db_path"] = str(workdir / "fb.db")
    app = api_mod.create_app(cfg)
    return app, TestClient(app)


def test_feedback_records_and_validates(workdir):
    app, client = _client(workdir)
    state = app.state.lf
    # 建一个任务
    from longflow import config as cm
    scfg = cm.load_scenario("team_ops")
    for pat in scfg.get("knowledge", []) or []:
        rag.load_knowledge_path(state.conn, pat, "team_ops")
    r = client.post("/api/tasks", json={"goal": "出差住宿标准是多少？",
                                        "scenario": "team_ops", "slots": {}})
    tid = r.json()["task"]["id"]

    # 非法 verdict / category 拒绝
    bad = client.post(f"/api/tasks/{tid}/feedback", json={"verdict": "nope"})
    assert bad.status_code == 422
    bad2 = client.post(f"/api/tasks/{tid}/feedback", json={"verdict": "answer_wrong", "category": "hacker"})
    assert bad2.status_code == 422

    # 答案有误 + 归因 + 说明
    ok = client.post(f"/api/tasks/{tid}/feedback", json={
        "verdict": "answer_wrong", "category": "generation", "comment": "金额不对"})
    assert ok.status_code == 200
    fid = ok.json()["feedback_id"]
    assert fid

    # 已解决
    client.post(f"/api/tasks/{tid}/feedback", json={"verdict": "resolved"})

    rows = db.list_feedback(state.conn, tid)
    verdicts = {x["verdict"] for x in rows}
    assert {"answer_wrong", "resolved"} <= verdicts
    wrong = [x for x in rows if x["verdict"] == "answer_wrong"][0]
    assert wrong["category"] == "generation" and "金额不对" in wrong["comment"]

    # 反馈不得自动写入知识库（chunks 不随反馈增加反馈内容）
    hits = rag.search(state.conn, "金额不对", scenario="team_ops")
    assert all("金额不对" not in (c.get("text") or "") for c in hits), "反馈内容不得自动入库"


def test_feedback_unknown_task_404(workdir):
    _, client = _client(workdir)
    r = client.post("/api/tasks/t_nonexistent/feedback", json={"verdict": "resolved"})
    assert r.status_code == 404
