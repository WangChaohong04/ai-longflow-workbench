"""Batch1：RecoveryWorker 经 FastAPI lifespan 接入；任务创建后后台推进。"""
from tests.conftest import workdir  # noqa: F401

from fastapi.testclient import TestClient

from longflow import api as api_mod
from longflow import config as cfg_mod, db


def _make_app(workdir):
    cfg = cfg_mod.load_config()
    cfg["db_path"] = str(workdir / "lf.db")
    cfg["limits"]["recovery_interval_seconds"] = 0.2
    return api_mod.create_app(cfg)


def test_lifespan_starts_worker_and_background_advances(workdir):
    app = _make_app(workdir)
    state = app.state.lf
    # 启动前无 worker
    assert state.worker is None
    with TestClient(app) as client:
        # lifespan startup 已启动 RecoveryWorker
        assert state.worker is not None and state.worker._thread is not None

        # 加载知识
        from longflow import rag
        state.conn.execute("SELECT COUNT(*) AS n FROM knowledge_chunks").fetchone()
        import longflow.config as cm
        scfg = cm.load_scenario("team_ops")
        for pat in scfg.get("knowledge", []) or []:
            rag.load_knowledge_path(state.conn, pat, "team_ops")

        # 创建一个纯查询任务（无审批），应被后台推进到完成
        r = client.post("/api/tasks", json={
            "goal": "出差住宿标准是多少？", "scenario": "team_ops", "slots": {}})
        assert r.status_code == 200
        tid = r.json()["task"]["id"]

        # 轮询等待后台 worker 推进（不手动 tick）
        import time
        status = None
        for _ in range(50):
            d = client.get(f"/api/tasks/{tid}").json()
            status = d["task"]["status"]
            if status in ("completed", "failed"):
                break
            time.sleep(0.1)
        assert status == "completed", f"后台 worker 应推进任务完成，实际 {status}"

    # shutdown 后 worker 停止
    assert state.worker is None
