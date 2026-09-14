"""FastAPI 应用：REST API + 静态工作台。"""
from __future__ import annotations

import json
import time
import threading
import re
from pathlib import Path
from typing import Any

import httpx
from contextlib import asynccontextmanager
from fastapi import Body, Depends, FastAPI, HTTPException, Request
from .auth import Authenticator, Principal, AuthError, principal_dependency
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


from . import config as cfg_mod
from . import db, events, orchestrator, rag
from .router import RouterAgent
from . import domains as dom_mod, importer, confirmation as confirm_mod
from .models import Task
from .plugins import loader as plugin_loader
from .tools import ToolRegistry, ToolRuntime, register_core_tools
from .workers import RecoveryWorker

WEB_DIR = Path(__file__).resolve().parent / "web"


# ---- 轻量请求限流（进程内滑动窗口；按 key 隔离，秒级互斥） ----
_RATE_WINDOWS: dict = {}
_RATE_LOCK = threading.Lock()


def _safe_domain(scenario):
    """从客户端 scenario 中提取唯一安全的领域名（剥除 user:/plugin: 等命名空间）。

    预览端点只检索服务端管理的全局领域知识（如 "team_ops"），绝不按客户端传入的
    user:<workspace>:... 命名空间检索，避免跨工作区泄露用户导入知识。
    """
    seg = str(scenario or "").split(":")[-1].strip()
    if re.fullmatch(r"[A-Za-z0-9_\-]+", seg):
        return seg
    return "team_ops"


def _rate_limited(key: str, *, limit: int, window_seconds: float) -> None:
    """滑动窗口限流；超限抛 AuthError(429)，由全局异常处理器转 JSON。"""
    now = time.monotonic()
    with _RATE_LOCK:
        q = _RATE_WINDOWS.setdefault(key, [])
        while q and now - q[0] > window_seconds:
            q.pop(0)
        if len(q) >= int(limit):
            raise AuthError("rate_limited", "请求过于频繁，请稍后再试", status=429)
        q.append(now)



class CreateTask(BaseModel):
    goal: str
    # 兼容模式：显式 scenario 走旧静态模板路径；缺省走 Router+Coordinator 自动识别
    scenario: str | None = None
    slots: dict[str, Any] | None = None
    auto_route: bool = True


class Message(BaseModel):
    text: str
    slots: dict[str, Any] | None = None


class Decision(BaseModel):
    decision: str  # approved | rejected


class Feedback(BaseModel):
    verdict: str  # resolved | unresolved | answer_wrong
    category: str | None = None
    comment: str | None = None


class RouteRequest(BaseModel):
    text: str
    context: dict[str, Any] | None = None


class ImportText(BaseModel):
    filename: str
    content: str
    workspace: str = "default"
    owner: str = "user"
    domain: str = "team_ops"
    version: str = ""


class ActivateDoc(BaseModel):
    by: str = "user"


class RetryBranch(BaseModel):
    branch: str | None = None


class TrialQuery(BaseModel):
    query: str
    top_k: int = 3


class SubagentCall(BaseModel):
    subagent: str
    query: str = ""
    target_sites: list[str] | None = None
    allowed_domains: list[str] | None = None
    required_fields: list[str] | None = None
    max_sources: int = 8
    extra: dict[str, Any] | None = None
    scenario: str = "team_ops"


class SuggestImprovements(BaseModel):
    run_id: str | None = None
    attributions: list[str] | None = None
    context: dict[str, Any] | None = None


class EvalImprovement(BaseModel):
    eval_result: dict[str, Any]


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
        self.authenticator = Authenticator(cfg)
        self.worker = None
        self.registry = ToolRegistry()
        register_core_tools(self.registry)
        self.plugins = plugin_loader.load_plugins(cfg)
        for tool_spec, lp in plugin_loader.collect_tools(self.plugins):
            # 插件工具重名默认报错（不得顶替核心工具/自降 risk）；冲突则隔离该工具。
            try:
                self.registry.register(tool_spec, data_dir=lp.data_dir,
                                       config=(cfg.get("plugins", {}).get(lp.name, {}) or {}).get("config", {}))
            except ValueError as exc:
                target = next((x for x in self.plugins if getattr(x, "name", None) == getattr(lp, "name", None)), None)
                if target is not None:
                    target.error = (getattr(target, "error", None) or "") + f" 工具名冲突已隔离: {exc}"
        # 插件注册领域（编程式 Domain Pack）：核心不写死任何领域
        try:
            plugin_packs = [p for p, _lp in plugin_loader.collect_domains(self.plugins)]
            self.domain_registry = dom_mod.load_default_registry(plugin_domains=plugin_packs)
        except Exception:  # noqa: BLE001 - 插件领域异常不拖垮核心
            self.domain_registry = dom_mod.load_default_registry()
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
        # 请求线程使用 AppState 连接（SQLite WAL + check_same_thread=False）。
        return orchestrator.Engine(self.conn, self.cfg, self.runtime)

    def worker_engine(self) -> orchestrator.Engine:
        # 后台 worker 在独立线程：新建独立连接与独立 ToolRuntime，
        # 使编排读写、工具执行、事件落库都走 worker 连接，不跨线程共享请求连接。
        conn = db.connect(self.cfg["db_path"])
        runtime = ToolRuntime(
            conn, self.registry,
            timeout_seconds=self.cfg["limits"].get("tool_timeout_seconds", 30),
            http=self.runtime.http,
        )
        return orchestrator.Engine(conn, self.cfg, runtime)


def create_app(cfg: dict | None = None) -> FastAPI:
    cfg = cfg or cfg_mod.load_config()
    state = AppState(cfg)
    require_principal = principal_dependency(state.authenticator)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 后台恢复循环：进程启动即恢复 in-flight 任务；关闭时停止。
        _lim = cfg.get("limits", {})
        interval = float(_lim.get("recovery_interval_seconds",
                             _lim.get("tick_interval_seconds", 1.0)))
        state.worker = RecoveryWorker(
            state.worker_engine, interval_seconds=interval,
            max_parallel=int(_lim.get("worker_concurrency", 4)))
        state.worker.start()
        try:
            yield
        finally:
            if state.worker:
                state.worker.stop()
                state.worker = None

    app = FastAPI(title="LongFlow", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(AuthError)
    async def _auth_err(request, exc: AuthError):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=exc.status,
                            content={"error": exc.message, "code": exc.code})
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
        # 有效驱动以运行时实例为准：配置 openai_compatible 但缺 key/不可达会回退本地。
        try:
            eff = state.engine().driver
        except Exception:  # noqa: BLE001
            eff = None
        from . import model_ledger as ml
        try:
            ml.init_ledger(state.conn)
            calls = state.conn.execute("SELECT COUNT(*) AS n FROM llm_calls").fetchone()["n"]
            fb = state.conn.execute("SELECT COUNT(*) AS n FROM llm_calls WHERE fallback=1").fetchone()["n"]
            fails = state.conn.execute("SELECT COUNT(*) AS n FROM llm_calls WHERE ok=0").fetchone()["n"]
        except Exception:  # noqa: BLE001
            calls = fb = fails = 0
        configured = cfg["llm"].get("driver", "local")
        return {
            "ok": True, "version": "0.1.0",
            "configured_driver": configured,
            "effective_driver": getattr(eff, "name", "unknown"),
            "degraded_to_local": getattr(eff, "name", None) == "local" and configured != "local",
            "llm_calls": calls, "llm_fallbacks": fb, "llm_failures": fails,
            "auth_mode": "local_single_user" if state.authenticator.local_dev else "token",
        }

    @app.get("/api/config")
    def get_config():
        out = cfg_mod.public_config(cfg)
        out["plugins"] = [p.to_dict() for p in state.plugins]
        out["tools"] = state.registry.names()
        return out

    def _scoped_task(task_id: str, principal: Principal):
        task = db.get_task(state.conn, task_id)
        if task is None:
            raise HTTPException(404, {"error": "任务不存在", "code": "not_found"})
        row = state.conn.execute("SELECT workspace, owner FROM tasks WHERE id=?",
                                 (task.root_id,)).fetchone()
        if not principal.is_admin and row["workspace"] != principal.workspace:
            raise HTTPException(404, {"error": "任务不存在", "code": "not_found"})
        if not principal.is_admin and row["owner"] != principal.user:
            raise HTTPException(403, {"error": "无权访问该任务", "code": "forbidden"})
        return task

    @app.post("/api/tasks")
    def create_task(body: CreateTask = Body(...), principal: Principal = Depends(require_principal)):
        engine = state.engine()
        # 默认主链路：Router 识别领域/风险/槽位 -> 缺失则 waiting_user ->
        # 激活 Domain Pack -> Coordinator 动态任务图 -> 固定 Subagent。
        # 只做快速的"建图/物化/落库"，把耗时的节点执行交给后台 RecoveryWorker，
        # 因此本接口立即返回任务 ID，不被慢工具/模型调用阻塞。
        try:
            if not body.scenario and body.auto_route:
                reg = getattr(state, "domain_registry", None) or dom_mod.load_default_registry()
                state.domain_registry = reg
                res = engine.create_domain_goal(
                    body.goal, workspace=principal.workspace, owner=principal.user,
                    registry=reg, context=(body.slots or {}).get("__context__"))
            else:
                # 兼容模式：显式 scenario -> 旧静态模板路径
                scenario = body.scenario or "team_ops"
                try:
                    cfg_mod.load_scenario(scenario)
                except FileNotFoundError:
                    raise HTTPException(404, {"error": f"未知场景: {scenario}",
                                              "code": "not_found"})
                _ensure_knowledge(state, scenario)
                res = engine.create_goal(body.goal, scenario, body.slots,
                                         workspace=principal.workspace, owner=principal.user)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - 首次调度/持久化失败如实记录，不静默吞掉
            try:
                events.emit(state.conn, events.ERROR, task_id="", actor="create_task",
                            detail={"error": f"scheduling_failed:{exc}"})
            except Exception:  # noqa: BLE001
                pass
            raise HTTPException(500, {"error": "任务创建失败，请重试", "code": "create_failed"})

        rid = res.get("task_id")
        # 记录“已持久化并调度后台执行”事件；实际推进由 worker 负责。
        events.emit(state.conn, events.TASK_STATUS, task_id=rid,
                    actor="create_task",
                    detail={"status": "scheduled",
                            "verdict": res.get("verdict"),
                            "worker": "recovery" if state.worker else "ondemand"})
        return _task_detail(db.get_task(state.conn, rid))

    @app.get("/api/tasks")
    def list_tasks(principal: Principal = Depends(require_principal)):
        if principal.is_admin:
            roots = db.list_tasks(state.conn, root_only=True)
        else:
            roots = db.list_roots(state.conn, workspace=principal.workspace)
            roots = [t for t in roots if _owner(state, t) == principal.user]
        return {"tasks": [t.to_dict() for t in roots]}

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str, principal: Principal = Depends(require_principal)):
        task = _scoped_task(task_id, principal)
        target = db.get_task(state.conn, task.root_id)
        return _task_detail(target)

    @app.post("/api/tasks/{task_id}/retry-branch")
    def retry_branch(task_id: str, body: RetryBranch = Body(...),
                     principal: Principal = Depends(require_principal)):
        task = _scoped_task(task_id, principal)
        engine = state.engine()
        try:
            return engine.retry_branch(task.root_id, body.branch)
        except KeyError:
            raise HTTPException(404, {"error": "任务不存在", "code": "not_found"})

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str, principal: Principal = Depends(require_principal)):
        task = _scoped_task(task_id, principal)
        engine = state.engine()
        return engine.cancel(task.root_id)

    @app.post("/api/tasks/{task_id}/message")
    def post_message(task_id: str, body: Message = Body(...),
                     principal: Principal = Depends(require_principal)):
        task = _scoped_task(task_id, principal)
        engine = state.engine()
        engine.user_message(task.root_id, body.text, body.slots)
        return _task_detail(db.get_task(state.conn, task.root_id))

    @app.post("/api/approvals/{approval_id}/decide")
    def decide(approval_id: str, body: Decision = Body(...),
               principal: Principal = Depends(require_principal)):
        row = state.conn.execute(
            "SELECT a.task_id FROM approvals a JOIN tasks t ON t.id=a.task_id WHERE a.id=?",
            (approval_id,)).fetchone()
        if row is None:
            raise HTTPException(404, {"error": "审批不存在", "code": "not_found"})
        _scoped_task(row["task_id"], principal)
        engine = state.engine()
        try:
            return engine.decide_approval(approval_id, body.decision, by=principal.user)
        except FileNotFoundError:
            raise HTTPException(404, {"error": "审批不存在", "code": "not_found"})


    @app.post("/api/tasks/{task_id}/feedback")
    def submit_feedback(task_id: str, body: Feedback = Body(...),
                        principal: Principal = Depends(require_principal)):
        task = _scoped_task(task_id, principal)
        allowed_v = {"resolved", "unresolved", "answer_wrong"}
        allowed_c = {"retrieval", "generation", "tool", "routing", "ui", "other", None}
        if body.verdict not in allowed_v:
            raise HTTPException(422, {"error": f"非法反馈判定: {body.verdict}", "code": "bad_request"})
        if body.category not in allowed_c:
            raise HTTPException(422, {"error": f"非法归因分类: {body.category}", "code": "bad_request"})
        root = db.get_task(state.conn, task.root_id)
        fid = db.add_feedback(state.conn, root_id=task.root_id, task_id=task.root_id,
                              verdict=body.verdict, category=body.category,
                              comment=(body.comment or None))
        events.emit(state.conn, events.MESSAGE, task_id=task.root_id, actor="user",
                    detail={"feedback": True, "verdict": body.verdict,
                            "category": body.category})
        return {"feedback_id": fid, "ok": True,
                "note": "反馈已记录；未复核的反馈不会自动写入知识库。"}

    # ---- 受控自进化（候选只登记 proposed；评测/审核/灰度/回滚均需管理员）----

    @app.get("/api/improvements")
    def list_improvements(status: str | None = None,
                          principal: Principal = Depends(require_principal)):
        from . import improvements as impr
        return {"improvements": impr.list_improvements(state.conn, status)}

    @app.post("/api/improvements/suggest")
    def suggest_improvements(body: SuggestImprovements,
                             principal: Principal = Depends(require_principal)):
        """根据归因生成候选改进——**只登记 proposed，绝不自动生效/改 prompt/改权限**。"""
        from . import improvements as impr
        state.authenticator.require_admin(principal)
        attrs = body.attributions or []
        if not attrs and body.run_id:
            row = state.conn.execute(
                "SELECT attributions_json FROM run_telemetry WHERE id=?",
                (body.run_id,)).fetchone()
            if row:
                import json as _json
                attrs = _json.loads(row["attributions_json"] or "[]")
        ids = impr.suggest_improvements(state.conn, attrs, context=body.context or {})
        return {"created": ids, "status": "proposed",
                "note": "候选仅登记，未改变任何 prompt/知识/权限/代码；须经独立评测+人工审核+灰度。"}

    @app.post("/api/improvements/{improvement_id}/evaluate")
    def evaluate_improvement(improvement_id: str, body: EvalImprovement,
                             principal: Principal = Depends(require_principal)):
        from . import improvements as impr
        state.authenticator.require_admin(principal)
        try:
            impr.set_evaluation(state.conn, improvement_id, body.eval_result, by=principal.user)
        except FileNotFoundError:
            raise HTTPException(404, {"error": "候选不存在", "code": "not_found"})
        return {"ok": True, "status": "evaluated"}

    @app.post("/api/improvements/{improvement_id}/approve")
    def approve_improvement(improvement_id: str,
                            principal: Principal = Depends(require_principal)):
        from . import improvements as impr
        state.authenticator.require_admin(principal)
        try:
            impr.approve(state.conn, improvement_id, by=principal.user)
        except FileNotFoundError:
            raise HTTPException(404, {"error": "候选不存在", "code": "not_found"})
        return {"ok": True, "status": "approved"}

    @app.post("/api/improvements/{improvement_id}/rollout")
    def rollout_improvement(improvement_id: str,
                            principal: Principal = Depends(require_principal)):
        from . import improvements as impr
        state.authenticator.require_admin(principal)
        try:
            impr.roll_out(state.conn, improvement_id, by=principal.user)
        except FileNotFoundError:
            raise HTTPException(404, {"error": "候选不存在", "code": "not_found"})
        except PermissionError as exc:
            raise HTTPException(409, {"error": str(exc), "code": "not_approved"})
        return {"ok": True, "status": "rolled_out",
                "note": "灰度标记已记录；是否真正应用由人工/部署决定，系统不自动改代码/prompt/权限。"}

    @app.post("/api/improvements/{improvement_id}/rollback")
    def rollback_improvement(improvement_id: str,
                             principal: Principal = Depends(require_principal)):
        from . import improvements as impr
        state.authenticator.require_admin(principal)
        try:
            impr.roll_back(state.conn, improvement_id, by=principal.user)
        except FileNotFoundError:
            raise HTTPException(404, {"error": "候选不存在", "code": "not_found"})
        return {"ok": True, "status": "rolled_back"}

    @app.get("/api/runs")
    def list_runs(principal: Principal = Depends(require_principal)):
        state.authenticator.require_admin(principal)
        rows = state.conn.execute(
            "SELECT * FROM run_telemetry ORDER BY created_at DESC LIMIT 200").fetchall()
        return {"runs": [dict(r) for r in rows]}

    @app.post("/api/grants")
    def add_grant(body: Grant = Body(...), principal: Principal = Depends(require_principal)):
        state.authenticator.require_admin(principal)  # 仅管理员/授权角色
        from .permissions import add_grant as _add
        gid = _add(state.conn, scope=body.scope, tool_name=body.tool_name,
                   object_pattern=body.object_pattern, max_count=body.max_count)
        return {"grant_id": gid}

    @app.get("/api/domains")
    def list_domains():
        reg = getattr(state, "domain_registry", None) or dom_mod.load_default_registry()
        state.domain_registry = reg
        return {"domains": [p.to_dict() for p in reg.all()]}

    @app.get("/api/capabilities")
    def capabilities(principal: Principal = Depends(require_principal)):
        """能力状态总览（脱敏：只含 enabled/ready/说明，绝不含端点/密钥）。"""
        from . import report as rep
        return rep.capability_overview(state)

    @app.get("/api/tasks/{task_id}/export")
    def export_task(task_id: str, format: str = "md",
                    principal: Principal = Depends(require_principal)):
        from fastapi.responses import PlainTextResponse
        from . import report as rep
        task = _scoped_task(task_id, principal)
        root = db.get_task(state.conn, task.root_id)
        if root is None:
            raise HTTPException(404, {"error": "任务不存在", "code": "not_found"})
        rows = rep.gather_evidence(state.conn, root.id)
        failures = (root.result or {}).get("failures") or []
        if format == "csv":
            data = rep.export_csv(root, rows)
            return PlainTextResponse("\ufeff" + data, media_type="text/csv",
                                     headers={"Content-Disposition": 'attachment; filename="task.csv"'})
        text = rep.export_markdown(root, rows, failures)
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8",
                                 headers={"Content-Disposition": 'attachment; filename="task.md"'})

    @app.post("/api/route")
    def route(body: RouteRequest = Body(...)):
        """轻量总路由预览：识别领域/风险/缺失/是否需要用户确认（不执行任何动作）。"""
        reg = getattr(state, "domain_registry", None) or dom_mod.load_default_registry()
        state.domain_registry = reg
        decision = RouterAgent(reg).route(body.text, context=body.context or {})
        return decision.to_dict()

    @app.get("/api/subagents")
    def list_subagents():
        from . import subagents as sa
        return {"subagents": [s.to_dict() for s in sa.all_specs()]}

    @app.post("/api/subagents/run")
    def run_subagent(body: SubagentCall = Body(...),
                     principal: Principal = Depends(require_principal)):
        """运行一个固定能力 subagent（研究型，只读）。返回统一证据，不做最终决策。

        - 认证：必须登录；知识/工作区范围以服务端 principal 为准；
        - 限流：按用户滑动窗口（subagent_rpm，默认 10 次/分钟）；
        - 审计：每次调用生成独立 run id 并写入 subagent_runs，不共用固定 "preview"；
        - 不信任客户端 scenario / workspace / 域名白名单：域名由服务端配置决定，
          检索 scope 绑定 principal.workspace。
        """
        from . import subagents as sa_mod
        from .subagent_runner import SubagentRunner
        _rate_limited(f"subagent:{principal.user}",
                      limit=int(state.cfg.get("limits", {}).get("subagent_rpm", 10)),
                      window_seconds=60)
        if sa_mod.get_spec(body.subagent) is None:
            raise HTTPException(422, {"error": f"未知 subagent: {body.subagent}",
                                      "code": "unknown_subagent"})
        # 服务端决定检索范围：仅查全局领域知识，剥除客户端传入的任何 user:/plugin: 命名空间。
        scenario = _safe_domain(body.scenario)
        req = sa_mod.SubagentRequest(
            query=body.query,
            # 域名白名单由服务端配置/领域包决定；客户端传入的 target_sites/allowed_domains 不作为越权来源
            target_sites=[], allowed_domains=[],
            required_fields=body.required_fields or [],
            max_sources=min(int(body.max_sources or 8), 20),
            extra=body.extra or {})
        runner = SubagentRunner(state.runtime, cfg=state.cfg)
        run_id = db.new_id("run")
        start = time.monotonic()
        try:
            res = runner.run(body.subagent, req, task_id=run_id, root_id=run_id,
                             scenario=scenario)
            db.record_subagent_run(
                state.conn, user=principal.user, workspace=principal.workspace,
                subagent=body.subagent, query=body.query, ok=bool(res.ok), source="api",
                error=(res.error or "") if not res.ok else "",
                result_summary=json.dumps({"evidence": len(res.evidence) or 0,
                                           "findings": res.findings or {}},
                                          ensure_ascii=False)[:2000],
                duration_ms=int((time.monotonic() - start) * 1000))
            out = res.to_dict()
            out["run_id"] = run_id
            return out
        except Exception as exc:  # noqa: BLE001 - 记录后如实失败，不静默吞掉
            db.record_subagent_run(
                state.conn, user=principal.user, workspace=principal.workspace,
                subagent=body.subagent, query=body.query, ok=False, source="api",
                error=f"execution_exception:{exc}", duration_ms=int((time.monotonic()-start)*1000))
            raise HTTPException(500, {"error": "subagent 执行失败", "code": "subagent_error"})

    @app.post("/api/knowledge/import")
    def import_text(body: ImportText = Body(...), principal: Principal = Depends(require_principal)):
        """文件/文本导入：解析并生成预览（不激活、不参与检索）。

        workspace/owner 以服务端凭据为准，忽略请求体；记录访问范围。"""
        _check_upload_size(state.cfg, body.content)
        try:
            doc = importer.parse_text(body.filename, body.content, version=body.version)
        except ValueError as exc:
            raise HTTPException(422, {"error": str(exc), "code": "import_parse_failed"})
        doc_id = importer.preview(state.conn, workspace=principal.workspace,
                                  owner=principal.user, domain=body.domain, doc=doc,
                                  access_scope="workspace")
        return importer.preview_summary(state.conn, doc_id)

    def _scoped_doc(doc_id: str, principal: Principal):
        row = state.conn.execute("SELECT * FROM imported_docs WHERE id=?", (doc_id,)).fetchone()
        if row is None:
            raise HTTPException(404, {"error": "文档不存在", "code": "not_found"})
        if not principal.is_admin and row["workspace"] != principal.workspace:
            raise HTTPException(404, {"error": "文档不存在", "code": "not_found"})
        if not principal.is_admin and row["owner"] != principal.user:
            raise HTTPException(403, {"error": "无权操作该文档", "code": "forbidden"})
        return row

    @app.post("/api/knowledge/upload")
    async def upload_knowledge(request: Request, principal: Principal = Depends(require_principal)):
        """multipart 文件上传：解析 -> 预览（不激活、不检索）。

        表单：file=<文件>，domain=<可选>，version=<可选>。
        workspace/uploader 以服务端凭据为准（不用请求体里的 workspace/owner/by）。
        """
        from . import multipart_read as mp
        body = await request.body()
        try:
            parsed = mp.parse_multipart(body, request.headers.get("content-type", ""))
        except mp.MultipartError as exc:
            raise HTTPException(422, {"error": str(exc), "code": "bad_multipart"})
        if not parsed["files"]:
            raise HTTPException(422, {"error": "缺少上传文件（字段名 file）",
                                      "code": "no_file"})
        f = parsed["files"][0]
        _check_upload_size(state.cfg, f["data"])
        domain = parsed["fields"].get("domain", "team_ops")
        version = parsed["fields"].get("version", "")
        try:
            doc = importer.parse_text(f["filename"], f["data"], version=version)
        except ValueError as exc:
            raise HTTPException(422, {"error": str(exc), "code": "import_parse_failed"})
        doc_id = importer.preview(state.conn, workspace=principal.workspace,
                                  owner=principal.user, domain=domain, doc=doc,
                                  access_scope="workspace")
        summary = importer.preview_summary(state.conn, doc_id)
        summary["uploader"] = principal.user  # 上传者=服务端凭据
        return summary

    @app.post("/api/knowledge/docs/{doc_id}/activate")
    def activate_doc(doc_id: str, body: ActivateDoc = Body(default=ActivateDoc()),
                     principal: Principal = Depends(require_principal)):
        """用户确认后激活知识（预览不入库，确认后才参与检索）。操作者取服务端凭据。"""
        _scoped_doc(doc_id, principal)
        try:
            n = importer.activate(state.conn, doc_id, by=principal.user)
        except FileNotFoundError:
            raise HTTPException(404, {"error": "文档不存在", "code": "not_found"})
        return {"activated": True, "chunks": n}

    @app.get("/api/knowledge/docs")
    def list_imported_docs(domain: str | None = None, status: str | None = None,
                           principal: Principal = Depends(require_principal)):
        # 工作区由凭据决定；普通成员只看自己的，管理员可看全工作区
        docs = importer.list_docs(state.conn, workspace=principal.workspace,
                                  domain=domain, status=status)
        if not principal.is_admin:
            docs = [d for d in docs if d.get("owner") == principal.user]
        return {"docs": docs}

    @app.get("/api/knowledge/docs/{doc_id}")
    def get_doc_preview(doc_id: str, principal: Principal = Depends(require_principal)):
        """查看单份导入文档的完整解析预览：内容/分段/页码/版本/解析警告。不参与检索。"""
        _scoped_doc(doc_id, principal)
        try:
            return importer.preview_summary(state.conn, doc_id, full=True)
        except FileNotFoundError:
            raise HTTPException(404, {"error": "文档不存在", "code": "not_found"})

    @app.post("/api/knowledge/docs/{doc_id}/trial")
    def trial_doc_query(doc_id: str, body: TrialQuery = Body(...),
                        principal: Principal = Depends(require_principal)):
        """在预-编文档内做"检索试问"（不激活、不改知识库），返回命中分段与引用。"""
        _scoped_doc(doc_id, principal)
        try:
            return importer.preview_trial_query(
                state.conn, doc_id, body.query, top_k=max(1, min(int(body.top_k), 10)))
        except FileNotFoundError:
            raise HTTPException(404, {"error": "文档不存在", "code": "not_found"})

    @app.get("/api/knowledge")
    def knowledge(q: str = "", domain: str | None = None,
                  principal: Principal = Depends(require_principal)):
        # 服务端按当前用户/workspace/domain 过滤，不信任前端传入的 scenario：
        # 只在该工作区激活的命名空间 user:<ws>:<domain> 内检索。
        scenario = f"user:{principal.workspace}:{domain}" if domain \
            else f"user:{principal.workspace}:%"
        if scenario.endswith("%"):
            results = rag.search(state.conn, q, scenario_like=scenario[:-1] + "%")
        else:
            results = rag.search(state.conn, q, scenario=scenario)
        return {"results": results, "scope": scenario}

    @app.get("/api/plugins")
    def plugins():
        return {"plugins": [p.to_dict() for p in state.plugins]}

    @app.post("/api/eval/run")
    def run_eval(principal: Principal = Depends(require_principal)):
        state.authenticator.require_admin(principal)
        _rate_limited(f"eval:{principal.user}",
                      limit=int(state.cfg.get("limits", {}).get("eval_rpm", 2)),
                      window_seconds=300)
        from tests.run_eval import run_all

        report = run_all()
        return report

    def _owner(state_, task_obj):
        r = state_.conn.execute("SELECT owner FROM tasks WHERE id=?", (task_obj.root_id,)).fetchone()
        return r["owner"] if r else None

    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

    return app


def _check_upload_size(cfg: dict, data) -> None:
    """上传/粘贴内容大小上限：超限返回 413，避免整份大文件被读入内存后解析。"""
    size = len(data) if data is not None else 0
    limit = int(cfg.get("limits", {}).get("max_upload_bytes", 20_000_000))
    if size > limit:
        raise HTTPException(413, {"error": f"内容过大（{size} 字节 > 上限 {limit}）",
                                  "code": "too_large"})


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
