"""FastAPI 应用：REST API + 静态工作台。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from fastapi import Body, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config as cfg_mod
from . import db, events, orchestrator, rag
from .models import Task
from .plugins import loader as plugin_loader
from .tools import ToolRegistry, ToolRuntime, register_core_tools

WEB_DIR = Path(__file__).resolve().parent / "web"


class CreateTask(BaseModel):
    goal: str
    scenario: str
    slots: dict[str, Any] | None = None


class Message(BaseModel):
    text: str
    slots: dict[str, Any] | None = None


class Decision(BaseModel):
    decision: str  # approved | rejected


class Grant(BaseModel):
    scope: str
    tool_name: str | None = None
    object_pattern: str | None = None
    max_count: int | None = None

class AppState:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.conn = db.connect(cfg["db_path"])
        db.init_db(self.conn)
        self.registry = ToolRegistry()
        register_core_tools(self.registry)
        self.plugins = plugin_loader.load_plugins(cfg)
        for tool_spec, lp in plugin_loader.collect_tools(self.plugins):
            # 插件清单负责命名；冲突时后者覆盖。插件数据目录随工具注入运行时。
            self.registry.register(tool_spec, data_dir=lp.data_dir,
                                   config=(cfg.get("plugins", {}).get(lp.name, {}) or {}).get("config", {}))
        # 插件知识注入（场景共享）
        for chunks, lp in plugin_loader.collect_knowledge(self.plugins):
            rag.load_plugin_knowledge(self.conn, chunks, scenario=f"plugin:{lp.name}")
        # 预热：加载所有已配置场景知识（幂等）
        try:
            rag.load_all(self.conn)
        except Exception:  # noqa: BLE001
            pass
        http_client = None
        try:
            http_client = httpx.Client(timeout=20)
        except Exception:  # noqa: BLE001
            http_client = None
        self.runtime = ToolRuntime(
            self.conn, self.registry,
            timeout_seconds=cfg["limits"].get("tool_timeout_seconds", 30),
            http=http_client,
        )

    def engine(self) -> orchestrator.Engine:
        return orchestrator.Engine(self.conn, self.cfg, self.runtime)


def create_app(cfg: dict | None = None) -> FastAPI:
    cfg = cfg or cfg_mod.load_config()
    state = AppState(cfg)
    app = FastAPI(title="LongFlow", version="0.1.0")
    app.state.lf = state


    def _task_detail(task: Task) -> dict:
        children = [c.to_dict() for c in db.list_children(state.conn, task.root_id)
                    if c.id != task.id]
        approvals = [
            dict(r) for r in state.conn.execute(
                "SELECT * FROM approvals WHERE task_id IN (SELECT id FROM tasks WHERE root_id=?)",
                (task.root_id,),
            ).fetchall()
        ]
        for a in approvals:
            a["args"] = json.loads(a.pop("args_json"))
            a.pop("args_hash", None)
        return {
            "task": task.to_dict(),
            "children": children,
            "approvals": approvals,
            "events": events.list_events(state.conn, task.id),
        }

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": "0.1.0", "driver": cfg["llm"]["driver"]}

    @app.get("/api/config")
    def get_config():
        out = cfg_mod.public_config(cfg)
        out["plugins"] = [p.to_dict() for p in state.plugins]
        out["tools"] = state.registry.names()
        return out

    @app.post("/api/tasks")
    def create_task(body: CreateTask = Body(...)):
        try:
            cfg_mod.load_scenario(body.scenario)
        except FileNotFoundError:
            raise HTTPException(404, {"error": f"未知场景: {body.scenario}",
                                      "code": "not_found"})
        engine = state.engine()
        # 场景知识首次加载（幂等：已存在则跳过）
        _ensure_knowledge(state, body.scenario)
        res = engine.create_goal(body.goal, body.scenario, body.slots)
        engine.tick(res["task_id"])
        return _task_detail(db.get_task(state.conn, res["task_id"]))

    @app.get("/api/tasks")
    def list_tasks():
        return {"tasks": [t.to_dict() for t in db.list_tasks(state.conn, root_only=True)]}

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str):
        task = db.get_task(state.conn, task_id)
        if task is None:
            raise HTTPException(404, {"error": "任务不存在", "code": "not_found"})
        target = db.get_task(state.conn, task.root_id)
        return _task_detail(target)

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str):
        engine = state.engine()
        task = db.get_task(state.conn, task_id)
        if task is None:
            raise HTTPException(404, {"error": "任务不存在", "code": "not_found"})
        return engine.cancel(task.root_id)

    @app.post("/api/tasks/{task_id}/message")
    def post_message(task_id: str, body: Message = Body(...)):
        engine = state.engine()
        task = db.get_task(state.conn, task_id)
        if task is None:
            raise HTTPException(404, {"error": "任务不存在", "code": "not_found"})
        engine.user_message(task.root_id, body.text, body.slots)
        return _task_detail(db.get_task(state.conn, task.root_id))

    @app.post("/api/approvals/{approval_id}/decide")
    def decide(approval_id: str, body: Decision = Body(...)):
        engine = state.engine()
        try:
            return engine.decide_approval(approval_id, body.decision)
        except FileNotFoundError:
            raise HTTPException(404, {"error": "审批不存在", "code": "not_found"})

    @app.post("/api/grants")
    def add_grant(body: Grant = Body(...)):
        from .permissions import add_grant as _add
        gid = _add(state.conn, scope=body.scope, tool_name=body.tool_name,
                   object_pattern=body.object_pattern, max_count=body.max_count)
        return {"grant_id": gid}

    @app.get("/api/knowledge")
    def knowledge(q: str = "", scenario: str | None = None):
        return {"results": rag.search(state.conn, q, scenario=scenario)}

    @app.get("/api/plugins")
    def plugins():
        return {"plugins": [p.to_dict() for p in state.plugins]}

    @app.post("/api/eval/run")
    def run_eval():
        from tests.run_eval import run_all

        report = run_all()
        return report

    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

    return app


def _ensure_knowledge(state: AppState, scenario: str) -> None:
    """幂等加载场景知识。"""
    already = state.conn.execute(
        "SELECT COUNT(*) AS n FROM knowledge_chunks WHERE scenario=?", (scenario,)
    ).fetchone()["n"]
    if already:
        return
    scenario_cfg = cfg_mod.load_scenario(scenario)
    for pattern in scenario_cfg.get("knowledge", []) or []:
        rag.load_knowledge_path(state.conn, pattern, scenario)
