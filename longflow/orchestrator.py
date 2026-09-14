"""主控 Agent 编排：入口闸门、任务图调度、权限工具调用、出口闸门。

设计原则：
- 模型提出下一步动作，Harness 验证并执行（权限/审批/幂等/预算/日志）。
- 任务独立于单次模型调用存在，状态持久化；run_root/tick 可重入，重启后继续。
- 一个分支等待审批不阻塞其他独立分支。
"""
from __future__ import annotations

import json
import math
import re
import threading
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

from . import config as cfg_mod
from . import db, events, llm, permissions, rag, verification
from . import coordinator as coord_mod
from . import domains as dom_mod
from . import subagents as sa_mod
from .subagent_runner import SubagentRunner
from . import improvements as impr
from . import confirmation as confirm_mod
from . import slots as slots_mod
from . import model_ledger as mledger
from .models import (
    CANCELLED,
    COMPLETED,
    FAILED,
    IN_PROGRESS,
    PENDING,
    READY,
    TERMINAL_STATUSES,
    WAITING_APPROVAL,
    WAITING_EVENT, WAITING_USER, WAITING_EXTERNAL,
    PARTIALLY_COMPLETED, RETRYING,
    ROLE_CONTROLLER,
    ROLE_EXECUTOR,
    ROLE_RESEARCHER,
    ROLE_VERIFIER,
)
from .tools import ToolRuntime, ToolResultInDoubt

# 同一数据库文件下按 root 序列化 tick：请求线程与后台 RecoveryWorker 不并发跑同一 root。
_TICK_LOCKS: dict = {}
_TICK_LOCKS_GUARD = threading.Lock()


def _root_lock(db_path: str, root_id: str) -> "threading.RLock":
    key = f"{db_path}:{root_id}"
    with _TICK_LOCKS_GUARD:
        lk = _TICK_LOCKS.get(key)
        if lk is None:
            lk = threading.RLock()
            _TICK_LOCKS[key] = lk
        return lk


EMOTION_HINTS = ["非常生气", "愤怒", "投诉", "差评"]

# 多轮消息分类（确定性、场景无关的关键词信号）
_NEW_TASK_HINTS = ["重新开始", "新任务", "另一件事", "换个问题", "新的问题", "帮我做另"]
_CONSTRAINT_CHANGE_HINTS = ["改成", "改为", "换成", "预算改成", "数量改", "调整为", "不是", "应该是",
                            "预算变", "改为预算", "预算提高", "预算降低"]


def classify_followup(text: str, *, is_waiting_clarify: bool, slot_updates: dict | None = None) -> str:
    """把一条用户消息归入多轮语义类别（供编排/记录，不做复杂 NLP）：

    - missing_info: 任务正等待澄清，消息在补缺失槽位；
    - constraint_change: 已给出的约束/槽位发生变化（需重新核验/重规划）；
    - new_task: 明确要另起任务；
    - follow_up: 其余追问/补充（沿用上下文）。
    """
    t = (text or "").strip()
    if any(h in t for h in _NEW_TASK_HINTS):
        return "new_task"
    if is_waiting_clarify:
        return "missing_info"
    # 显式槽位变更（与现有槽位同名且值不同）视为约束变更
    if slot_updates:
        return "constraint_change"
    if any(h in t for h in _CONSTRAINT_CHANGE_HINTS):
        return "constraint_change"
    return "follow_up"


class TaskGraphError(Exception):
    pass


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _interp(value, slots: dict, goal: str = ""):
    """{slots.xxx} / {goal} 占位插值；空值（None/""）的过滤条件自动剔除。"""
    if isinstance(value, str):
        def repl(m):
            token = m.group(1)
            if token == "goal":
                return goal
            v = slots.get(token[len("slots."):])
            return "" if v is None else str(v)
        return re.sub(r"\{(slots\.[a-zA-Z0-9_]+|goal)\}", repl, value)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            iv = _interp(v, slots, goal)
            if iv not in (None, ""):
                out[k] = iv
        return out
    if isinstance(value, list):
        return [_interp(v, slots, goal) for v in value]
    return value


def _coerce_numbers(args: dict) -> dict:
    """把数值型字符串参数还原为数字；剔除空字典/空字符串参数（模板插值后处理）。"""
    out = {}
    for k, v in (args or {}).items():
        if v is None:
            continue
        if isinstance(v, dict):
            if v:  # 空字典（如 filters: {}）不下发；非空（如 filters.category=咖啡馆）保留
                cleaned = {kk: _coerce_numbers({kk: vv})[kk] for kk, vv in v.items()
                           if vv not in (None, "")}
                if cleaned:
                    out[k] = cleaned
            continue
        if isinstance(v, str):
            if v == "":
                continue
            m = re.fullmatch(r"-?\d+", v)
            if m:
                out[k] = int(v)
                continue
            m = re.fullmatch(r"-?\d+\.\d+", v)
            if m:
                out[k] = float(v)
                continue
        out[k] = v
    return out


# 可恢复瞬时错误（网络/超时/外部源暂时不可达）：允许有界自动重试；
# source_not_configured 是配置缺口（非瞬时），不重试。
_RETRIABLE_ERRORS = ("timeout", "timed out", "search_unreachable", "source_unreachable",
                     "connection", "temporar", "502", "503", "504", "reset",
                     "subagent 执行异常")


def _is_retriable_error(error: str | None) -> bool:
    e = (error or "").lower()
    return any(k in e for k in _RETRIABLE_ERRORS)


def _dedup_conflicts(conflicts):
    out, seen = [], set()
    for c in conflicts:
        key = json.dumps(c, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key); out.append(c)
    return out


def _compose_coordinator_answer(root, layer_counts, failures, branches_ok, branches_failed) -> str:
    parts = [f"已完成「{root.objective}」的只读研究："]
    parts.append(f"已确认事实 {layer_counts['confirmed']} 条、推断 {layer_counts['inferred']} 条、"
                 f"未确认/观点 {layer_counts['unconfirmed']} 条、无法确认 {layer_counts['unanswerable']} 条。")
    if branches_ok or branches_failed:
        msg = []
        if branches_ok:
            msg.append("成功分支：" + "、".join(branches_ok))
        if branches_failed:
            msg.append("失败分支：" + "、".join(branches_failed) + "（已保留其余分支结果，可稍后重试）")
        parts.append("；".join(msg) + "。")
    if failures and not branches_failed:
        parts.append(f"有 {len(failures)} 个来源失败，结论为部分完成。")
    parts.append("最终选择/取舍由您决定；本系统不下单、不付款、不自动外发。")
    return "\n".join(parts)


class _SimpleRoute:
    """恢复路径用的轻量路由结果（已确认领域/槽位，直接进入物化）。"""
    def __init__(self, pack, slots):
        self.domains = [{"id": pack.id, "confidence": 1.0}]
        self.known_slots = {k: v for k, v in slots.items() if not k.startswith("__")}
        self.missing_slots = []
        self.risk = pack.risk
        self.mode = "activate_domain"
        self.needs_user = False
        self.question = ""
        self.options = []
        self.reason = "resume_after_clarify"

    def to_dict(self):
        return {"domains": self.domains, "mode": self.mode, "risk": self.risk,
                "known_slots": self.known_slots, "missing_slots": []}


def RouterAgent_extract(text):
    from .router import RouterAgent
    return RouterAgent.extract_entities(text)


class Engine:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: dict,
        runtime: ToolRuntime,
        driver: llm.BaseDriver | None = None,
    ):
        self.conn = conn
        self.cfg = cfg
        self.runtime = runtime
        self.driver = driver or llm.build_driver(cfg)
        self.limits = cfg.get("limits", {})
        try:
            mledger.init_ledger(self.conn)
        except Exception:  # noqa: BLE001
            pass
        self.budget = mledger.ModelBudget(self.conn, self.limits.get("max_llm_calls"))

    def _record_driver_reports(self, task) -> None:
        """抽取驱动真实 HTTP 调用记录（usage/token/重试/降级/错误）写入模型账本。

        以驱动边界的真实请求为准（含重试尝试与降级标记），模板/规则动作不在此列。
        token 不可得时保持 None，不写 0。
        """
        drain = getattr(self.driver, "drain_reports", None)
        if not callable(drain):
            return
        for rec in drain() or []:
            try:
                self.budget.check(task.root_id)
            except Exception:
                pass
            mledger.record_call(
                self.conn,
                driver=rec.get("driver", "unknown"),
                model=rec.get("model"),
                call_type=rec.get("call_type", "chat"),
                ok=bool(rec.get("ok")),
                fallback=bool(rec.get("fallback")),
                latency_ms=rec.get("latency_ms"),
                prompt_tokens=rec.get("prompt_tokens"),
                completion_tokens=rec.get("completion_tokens"),
                error=rec.get("error"),
                task_id=task.id, root_id=task.root_id,
            )
            # 降级/失败同时发可见事件（不再静默）
            if rec.get("fallback") or not rec.get("ok"):
                events.emit(
                    self.conn, events.LLM_CALL, task_id=task.id,
                    actor=f"agent:{task.agent_role}",
                    detail={"driver": rec.get("driver"), "model": rec.get("model"),
                            "real_model_call": True, "fallback": bool(rec.get("fallback")),
                            "ok": bool(rec.get("ok")),
                            "error": rec.get("error"),
                            "call_type": rec.get("call_type")},
                )

    # ---------- 创建任务与入口闸门 ----------

    def create_goal(self, goal_text: str, scenario: str, slots: dict | None = None,
                     *, workspace: str = "default", owner: str = "local") -> dict:
        scenario_cfg = cfg_mod.load_scenario(scenario)
        merged_slots = dict(slots or {})
        try:
            merged_slots.update(self.driver.extract_slots(goal_text, scenario_cfg.get("slots", [])))
        except AttributeError:
            pass
        for k, v in (scenario_cfg.get("slot_defaults", {}) or {}).items():
            merged_slots.setdefault(k, v)

        root_id = db.insert_task(
            self.conn,
            title=goal_text[:60],
            kind="goal",
            agent_role=ROLE_CONTROLLER,
            objective=goal_text,
            root_id="",
            slots={**merged_slots, "__scenario__": scenario},
            status=PENDING,
            workspace=workspace, owner=owner,
        )
        self.conn.execute("UPDATE tasks SET root_id=? WHERE id=?", (root_id, root_id))
        self.conn.commit()
        events.emit(self.conn, events.TASK_CREATED, task_id=root_id, actor="user",
                    detail={"goal": goal_text, "scenario": scenario, "slots": merged_slots})

        permissions.preauth_from_policy(self.conn, scenario_cfg.get("policies", {}), task_id=root_id)

        gate = self._gate_entry(root_id, goal_text, scenario_cfg, merged_slots)
        if gate["verdict"] == "clarify":
            db.update_task(self.conn, root_id, status=WAITING_USER,
                           result={"clarify": gate["missing_slots"], "gate": "entry",
                                   "wait_kind": "clarify",
                                   "message": "需要补充信息才能继续"})
            events.emit(self.conn, events.CLARIFY, task_id=root_id, actor="controller",
                        detail={"missing_slots": gate["missing_slots"]})
        elif gate["verdict"] == "handoff":
            db.update_task(self.conn, root_id, status=WAITING_EXTERNAL,
                           result={"handoff": True, "message": gate["message"], "gate": "entry",
                                   "wait_kind": "handoff"})
        else:
            self._materialize_plan(root_id, gate["plan"])
        return {"task_id": root_id, **gate}

    def _gate_entry(self, root_id: str, goal: str, scenario_cfg: dict, slots: dict) -> dict:
        emotion = any(h in goal for h in EMOTION_HINTS)
        # 计划始终由确定性规划产出（LocalDriver/外部驱动均本地规划），不计为真实模型调用。
        plan = self.driver.plan(goal, scenario_cfg, slots)
        events.emit(
            self.conn, events.GATE_ENTRY, task_id=root_id, actor="controller",
            detail={
                "intent": plan.get("intent"),
                "signals": plan.get("signals", {}),
                "emotion_signal": emotion,
                "handoff_requested": plan.get("handoff_requested"),
                "missing_slots": plan.get("missing_slots", []),
            },
        )

        if plan.get("handoff_requested"):
            channel = self.cfg.get("human_channel")
            if channel:
                return {"verdict": "handoff", "message": f"已按您要求转人工，渠道：{channel}"}
            return {"verdict": "handoff",
                    "message": "您要求人工处理，但当前系统未配置人工渠道，无法完成转接；请联系管理员配置后再试。"}

        if plan.get("intent") is None:
            # 意图不明确：尝试通用知识回答，不强行追问
            plan["tasks"] = scenario_cfg.get("subtask_templates", {}).get("规则", [])
            plan["intent"] = "通用咨询"

        if plan.get("missing_slots"):
            return {"verdict": "clarify", "missing_slots": plan["missing_slots"], "plan": plan}

        return {"verdict": "pass", "plan": plan}

    def _root_ws(self, root_id: str, default="default") -> str:
        r = self.conn.execute("SELECT workspace FROM tasks WHERE id=?", (root_id,)).fetchone()
        return r["workspace"] if r else default

    def _root_owner(self, root_id: str, default="local") -> str:
        r = self.conn.execute("SELECT owner FROM tasks WHERE id=?", (root_id,)).fetchone()
        return r["owner"] if r else default

    def _materialize_plan(self, root_id: str, plan: dict) -> None:
        root = db.get_task(self.conn, root_id)
        templates = plan.get("tasks", [])
        if not templates:
            templates = [{"role": ROLE_RESEARCHER, "objective": "检索并回答", "tool": "kb_search", "tool_args": {"query": "{goal}"}}]
        id_by_idx: dict[int, str] = {}
        for idx, tpl in enumerate(templates):
            tid = db.insert_task(
                self.conn,
                title=(tpl.get("title") or tpl.get("objective", f"子任务{idx+1}"))[:60],
                kind="subtask",
                agent_role=tpl.get("role", ROLE_RESEARCHER),
                objective=tpl.get("objective", ""),
                root_id=root_id,
                parent_id=root_id,
                depends_on=[],
                slots=dict(root.slots),
                status=PENDING,
                workspace=self._root_ws(root_id, "default"),
                owner=self._root_owner(root_id, "local"),
            )
            id_by_idx[idx] = tid
            self.conn.execute(
                "UPDATE tasks SET plan_json=? WHERE id=?",
                (json.dumps({
                    "tool_hint": tpl.get("tool"),
                    "tool_args": tpl.get("tool_args"),
                    "risk": tpl.get("risk", "low"),
                    "side_effect": tpl.get("side_effect", False),
                    "template_id": tpl.get("id"),
                    "template_index": idx,
                }, ensure_ascii=False), tid),
            )
        dep_pairs = []
        for idx, tpl in enumerate(templates):
            for dep_idx in tpl.get("depends", []) or []:
                if dep_idx not in id_by_idx:
                    raise TaskGraphError(f"子任务 {idx} 依赖不存在的任务索引 {dep_idx}")
                dep_pairs.append((id_by_idx[idx], id_by_idx[dep_idx]))
        self._check_cycle(dep_pairs)
        for tid, dep_id in dep_pairs:
            task = db.get_task(self.conn, tid)
            deps = task.depends_on + [dep_id]
            self.conn.execute("UPDATE tasks SET depends_on_json=? WHERE id=?",
                              (json.dumps(deps, ensure_ascii=False), tid))
        self.conn.commit()
        db.update_task(self.conn, root_id, status=IN_PROGRESS,
                       plan={"intent": plan.get("intent"), "children": list(id_by_idx.values())})
        events.emit(self.conn, events.TASK_STATUS, task_id=root_id, actor="controller",
                    detail={"status": IN_PROGRESS, "children": len(id_by_idx)})

    @staticmethod
    def _check_cycle(pairs: list[tuple[str, str]]) -> None:
        adj: dict[str, list[str]] = {}
        nodes = set()
        for a, b in pairs:  # a 依赖 b
            adj.setdefault(a, []).append(b)
            nodes.add(a)
            nodes.add(b)
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in nodes}

        def dfs(n: str) -> None:
            color[n] = GRAY
            for m in adj.get(n, []):
                if color.get(m) == GRAY:
                    raise TaskGraphError("任务依赖存在循环")
                if color.get(m) == WHITE:
                    dfs(m)
            color[n] = BLACK

        for n in nodes:
            if color[n] == WHITE:
                dfs(n)

    # ---------- 调度循环（可重入，重启后可反复调用） ----------

    def tick(self, root_id: str) -> dict:
        root = db.get_task(self.conn, root_id)
        if root is None:
            raise FileNotFoundError(root_id)
        if root.status in TERMINAL_STATUSES:
            return {"root_id": root_id, "status": root.status, "advanced": False}

        with _root_lock(self.cfg.get("db_path", ""), root_id):
            return self._tick_locked(root_id)

    def _tick_locked(self, root_id: str) -> dict:
        root = db.get_task(self.conn, root_id)
        # 重启恢复：回收崩溃遗留的陈旧 in_progress（正在运行的不会被误回收）
        try:
            db.reset_stale_inprogress(self.conn)
        except Exception:  # noqa: BLE001
            pass
        steps = 0
        max_steps = self.limits.get("max_steps", 30)
        advanced_any = False

        self._resume_approved(root_id)

        while steps < max_steps:
            root = db.get_task(self.conn, root_id)
            if root.status in TERMINAL_STATUSES:
                break
            children = db.list_children(self.conn, root_id)
            self._cascade_failures(children)
            children = db.list_children(self.conn, root_id)

            ready = self._ready_actions(children)
            if not ready:
                break
            research = [t for t in ready if t.plan.get("engine") == "coordinator"
                        and t.plan.get("node_kind") == "research"]
            width = max(1, min(16, int(self.limits.get("max_parallel_subagents", 3))))
            batch = research[:min(width, max_steps - steps)]
            path = self.conn.execute("PRAGMA database_list").fetchone()["file"]
            if len(batch) > 1 and path:
                # Each worker owns its SQLite connection and runtime. The parent owns the
                # root scheduling lock; workers never recursively acquire that lock.
                with ThreadPoolExecutor(max_workers=len(batch), thread_name_prefix="lf-branch") as pool:
                    advanced = list(pool.map(lambda t: self._run_parallel_node(path, t.id, root_id), batch))
                advanced_any = any(advanced) or advanced_any
                steps += len(batch)
            else:
                advanced_any = self._run_subtask(ready[0], root) or advanced_any
                steps += 1
            self._resume_approved(root_id)

        children = db.list_children(self.conn, root_id)
        cur_root = db.get_task(self.conn, root_id)
        if cur_root.status in TERMINAL_STATUSES:
            return {"root_id": root_id, "status": cur_root.status, "advanced": advanced_any}
        is_coord = (cur_root.plan or {}).get("engine") == "coordinator"
        if children and all(c.status in TERMINAL_STATUSES for c in children):
            if is_coord:
                self._gate_exit_coordinator(cur_root, children)
            else:
                self._gate_exit(cur_root)
        elif not children and root.status not in (WAITING_EVENT, WAITING_USER, WAITING_EXTERNAL, WAITING_APPROVAL):
            self._gate_exit(cur_root)

        return {"root_id": root_id, "status": db.get_task(self.conn, root_id).status,
                "advanced": advanced_any}

    def _run_parallel_node(self, path: str, task_id: str, root_id: str) -> bool:
        conn = db.connect(path)
        try:
            runtime = ToolRuntime(conn, self.runtime.registry, http=self.runtime.http,
                                  timeout_seconds=self.runtime.timeout_seconds)
            worker = Engine(conn, self.cfg, runtime)
            return worker._run_subtask(db.get_task(conn, task_id), db.get_task(conn, root_id))
        finally:
            conn.close()

    def _resume_approved(self, root_id: str) -> None:
        rows = self.conn.execute(
            """SELECT a.* FROM approvals a JOIN tasks t ON t.id=a.task_id
               WHERE t.root_id=? AND a.status IN ('approved','rejected')""",
            (root_id,),
        ).fetchall()
        for r in rows:
            task = db.get_task(self.conn, r["task_id"])
            if task is None or task.status != WAITING_APPROVAL:
                continue
            if r["status"] == "rejected":
                db.update_task(self.conn, task.id, status=FAILED,
                               result={"error": "审批被拒绝，动作未执行", "code": "approval_rejected",
                                       "approval_id": r["id"]})
                events.emit(self.conn, events.TASK_STATUS, task_id=task.id, actor="governance",
                            detail={"status": FAILED, "reason": "approval_rejected"})
            else:
                db.update_task(self.conn, task.id, status=READY, clear_schedule=True)
                events.emit(self.conn, events.RECOVERY, task_id=task.id, actor="runtime",
                            detail={"reason": "approval_granted_resume", "approval_id": r["id"]})

    def _cascade_failures(self, children: list) -> None:
        by_id = {c.id: c for c in children}
        for c in children:
            # Coordinator 动态图：研究分支允许部分失败，汇聚(normalize/verify/compare)
            # 基于成功分支证据继续，不因单个研究节点失败而级联整图。
            if ((c.plan or {}).get("engine") == "coordinator"
                    and (c.plan or {}).get("node_kind") in ("normalize", "verify", "compare")):
                continue
            if c.status in (PENDING, READY, IN_PROGRESS, WAITING_APPROVAL, WAITING_EVENT, WAITING_USER, WAITING_EXTERNAL):
                for dep in c.depends_on:
                    d = by_id.get(dep)
                    if d and d.status in (FAILED, CANCELLED):
                        db.update_task(
                            self.conn, c.id, status=FAILED,
                            result={"error": f"前置任务「{d.title}」{d.status}，依赖未满足",
                                    "reason": "dependency_failed"},
                        )
                        events.emit(self.conn, events.TASK_STATUS, task_id=c.id, actor="scheduler",
                                    detail={"status": FAILED, "reason": "dependency_failed",
                                            "failed_dep": dep})
                        break

    def _next_actionable(self, children: list):
        ready = self._ready_actions(children)
        return ready[0] if ready else None

    def _ready_actions(self, children: list) -> list:
        # 仅在所有依赖完成后调度 READY（PENDING 先转 READY）。IN_PROGRESS 不在此返回：
        # 它要么正被某 worker 执行（有锁/有领取），要么由 reset_stale_inprogress 回收为 READY。
        by_id = {d.id: d for d in children}
        ready = []
        for c in children:
            if c.status == PENDING:
                is_coord = ((c.plan or {}).get("engine") == "coordinator"
                            and c.plan.get("node_kind") in ("normalize", "verify", "compare"))
                def _dep_ok(dep):
                    d = by_id.get(dep)
                    if d is None:
                        return False
                    # 汇聚节点容忍研究分支失败（部分失败），但取消不满足
                    if is_coord:
                        return d.status in (COMPLETED, FAILED)
                    return d.status == COMPLETED
                deps_done = all(_dep_ok(dep) for dep in c.depends_on)
                if not deps_done:
                    continue
                db.update_task(self.conn, c.id, status=READY)
                c = db.get_task(self.conn, c.id)
            if c.status == RETRYING:
                # 恢复循环再次领取：有界重试的节点重新入队
                db.update_task(self.conn, c.id, status=READY)
                c = db.get_task(self.conn, c.id)
            if c.status == READY:
                ready.append(c)
        return ready

    # ---------- 子任务执行 ----------


    # ======================================================================
    # 领域驱动主链路（Router -> 澄清 -> Coordinator 动态任务图 -> 固定 Subagent）
    # 与旧 scenario 模板路径并存；默认 /api/tasks 走本路径，旧 scenario 显式选择仍兼容。
    # ======================================================================

    @staticmethod
    def _known_slot_lines(decision) -> list[str]:
        lines = []
        for k, v in (getattr(decision, "known_slots", {}) or {}).items():
            lines.append(f"{k} = {v}")
        return lines or ["（暂无已确认参数）"]

    def _pause_confirmation(self, root_id: str, reason_code: str, *, problem: str,
                            decision_needed: str, question: str, options=None,
                            confirmed_info=None, context=None, wait_status=None,
                            extra_result=None) -> dict:
        req = confirm_mod.build_confirmation(
            reason_code,
            confirmed_info=confirmed_info or [],
            problem=problem, decision_needed=decision_needed,
            options=options or [], question=question, context=context or {})
        payload = req.to_dict()
        result = {"wait_kind": reason_code, "confirmation": payload,
                  "message": question or decision_needed,
                  "question": question or decision_needed,
                  "options": [o["label"] for o in payload["options"]]}
        if extra_result:
            result.update(extra_result)
        db.update_task(self.conn, root_id,
                       status=wait_status or WAITING_USER, result=result)
        events.emit(self.conn, events.CLARIFY, task_id=root_id, actor="controller",
                    detail={"reason_code": reason_code,
                            "decision_needed": decision_needed,
                            "options": [o["id"] for o in payload["options"]]})
        return result

    def _pause_snapshot(self, root, *, completed_work=None, evidence_refs=None,
                        failures=None, open_questions=None, recovery_steps=None,
                        confirmation=None) -> dict:
        return {
            "paused_at": db.now(),
            "completed_work": completed_work or [],
            "evidence_refs": evidence_refs or [],
            "failures": failures or [],
            "open_questions": open_questions or [],
            "recovery_steps": recovery_steps or [],
            "confirmation": confirmation,
        }

    def create_domain_goal(self, goal_text: str, *, workspace: str = "default",
                           owner: str = "local", registry=None,
                           context: dict | None = None) -> dict:
        from .router import RouterAgent
        reg = registry or dom_mod.load_default_registry()
        decision = RouterAgent(reg).route(goal_text, context=context or {})

        root_id = db.insert_task(
            self.conn, title=goal_text[:60], kind="goal",
            agent_role=ROLE_CONTROLLER, objective=goal_text, root_id="",
            slots={"__domain__": (decision.domains[0]["id"] if decision.domains else ""),
                   **decision.known_slots},
            status=PENDING, workspace=workspace, owner=owner)
        self.conn.execute("UPDATE tasks SET root_id=? WHERE id=?", (root_id, root_id))
        route_info = decision.to_dict()
        events.emit(self.conn, events.GATE_ENTRY, task_id=root_id, actor="router",
                    detail={"intent": (decision.domains[0]["id"] if decision.domains else None),
                            "risk": decision.risk, "mode": decision.mode,
                            "missing_slots": decision.missing_slots,
                            "route": route_info})

        # 高风险 / 需确认：先 waiting_user（不自动执行），用 5 段结构化确认
        if decision.mode in ("clarify", "multi_domain", "handoff", "await_confirmation") \
                or decision.needs_user:
            wait = WAITING_EXTERNAL if decision.mode == "handoff" else WAITING_USER
            result = self._domain_pause_result(root_id, decision, route_info, wait_status=wait)
            return {"task_id": root_id, "verdict": decision.mode, "route": route_info,
                    "missing_slots": decision.missing_slots,
                    "confirmation": result.get("confirmation")}

        return self._materialize_domain(root_id, goal_text, decision, reg)

    def _domain_pause_result(self, root_id: str, decision, route_info: dict, *,
                             wait_status: str) -> dict:
        known = self._known_slot_lines(decision)
        mode = decision.mode
        if mode == "handoff":
            opts = [confirm_mod.ConfirmationOption("wait_human", "转人工渠道处理",
                        impact="暂停自动执行，由人工跟进，不产生外部副作用", risk="low"),
                    confirm_mod.ConfirmationOption("cancel", "取消",
                        impact="不执行任何动作", risk="low")]
            return self._pause_confirmation(
                root_id, confirm_mod.PAUSE_OUT_OF_SCOPE, wait_status=wait_status,
                problem=decision.question or "该任务需转人工/外部渠道，超出自动处理范围。",
                decision_needed="是否转人工渠道处理？",
                question="该任务需要人工交接，如何继续？", options=opts, confirmed_info=known,
                context={"route": route_info})
        if mode == "multi_domain":
            opts = [confirm_mod.ConfirmationOption("one", "先处理其中一个领域",
                        impact="范围收窄为单一领域，另一个稍后再提，避免跨域混做", risk="low"),
                    confirm_mod.ConfirmationOption("separate", "拆成多个任务分别执行",
                        impact="各自独立核验与审批，耗时增加但边界清晰", risk="low"),
                    confirm_mod.ConfirmationOption("cancel", "取消",
                        impact="不创建执行任务", risk="low")]
            return self._pause_confirmation(
                root_id, confirm_mod.PAUSE_AMBIGUOUS_GOAL, wait_status=wait_status,
                problem=decision.question or "目标涉及多个领域，无法安全地一次性自动执行。",
                decision_needed="希望如何拆分/优先处理哪个领域？",
                question="这涉及多个领域，您希望怎么进行？", options=opts,
                confirmed_info=known,
                context={"domains": [d.get("id") for d in decision.domains]})
        if mode == "await_confirmation":
            opts = [
                confirm_mod.ConfirmationOption("confirm", "继续（我了解这是高风险动作）",
                    impact="进入执行前仍会对每个不可逆动作逐次审批，不自动下单/支付/外发",
                    risk="high"),
                confirm_mod.ConfirmationOption("modify", "补充/调整预算、数量或范围",
                    impact="暂停执行，先澄清，不产生副作用", risk="low"),
                confirm_mod.ConfirmationOption("cancel", "取消", impact="不执行", risk="low"),
            ]
            return self._pause_confirmation(
                root_id, confirm_mod.PAUSE_IRREVERSIBLE, wait_status=wait_status,
                problem=decision.question or "该任务涉及资金/下单/发送等不可逆高风险动作。",
                decision_needed="在补齐信息后，是否仍要推进该高风险任务？",
                question="这是高风险任务，确认要继续吗？", options=opts, confirmed_info=known,
                context={"risk": decision.risk, "route": route_info})
        miss = ", ".join(m.get("name", "?") for m in (decision.missing_slots or [])) or "关键信息"
        opts = [confirm_mod.ConfirmationOption("answer", "我现在补充",
                    impact="补齐后仅激活所需研究/执行分支", risk="low"),
                confirm_mod.ConfirmationOption("compare_all", "都可以/全部比较",
                    impact="对该维度展开多分支并行研究，再汇总对照，不替您做最终选择",
                    risk="low"),
                confirm_mod.ConfirmationOption("cancel", "取消", impact="不继续", risk="low")]
        return self._pause_confirmation(
            root_id, confirm_mod.PAUSE_MISSING_INFO, wait_status=wait_status,
            problem=decision.question or f"缺少会影响范围/结论的信息：{miss}。",
            decision_needed=f"请补充：{miss}",
            question=decision.question or f"请补充 {miss}",
            options=opts, confirmed_info=known,
            extra_result={"clarify": decision.missing_slots, "route": route_info})

    def _materialize_domain(self, root_id: str, goal_text: str, decision, reg) -> dict:
        domain_id = decision.domains[0]["id"] if decision.domains else None
        pack = reg.get(domain_id) if domain_id else None
        slots = dict(decision.known_slots)
        if pack is None:
            # 无可激活领域：转通用规则/澄清
            db.update_task(self.conn, root_id, status=WAITING_USER,
                           result={"wait_kind": "clarify", "route": decision.to_dict(),
                                   "message": "未能确定处理领域，请补充您想做什么。"})
            return {"task_id": root_id, "verdict": "clarify",
                    "missing_slots": decision.missing_slots}

        graph = coord_mod.Coordinator(pack).plan(
            goal_text, slots, missing_slots=decision.missing_slots)
        if graph.clarify:
            db.update_task(self.conn, root_id, status=WAITING_USER,
                           result={"wait_kind": "clarify", "route": decision.to_dict(),
                                   "clarify": graph.clarify,
                                   "message": "需要补充信息才能继续"})
            events.emit(self.conn, events.CLARIFY, task_id=root_id, actor="coordinator",
                        detail={"missing_slots": graph.clarify})
            return {"task_id": root_id, "verdict": "clarify",
                    "missing_slots": graph.clarify, "route": decision.to_dict()}

        id_by_key: dict[str, str] = {}
        for node in graph.nodes:
            spec = sa_mod.get_spec(node.subagent)
            tid = db.insert_task(
                self.conn, title=node.label[:60], kind="subtask",
                agent_role=f"subagent:{node.subagent}",
                objective=node.request.get("query", node.label),
                root_id=root_id, parent_id=root_id, depends_on=[],
                slots={"__domain__": pack.id, **slots}, status=PENDING,
                workspace=self._root_ws(root_id, "default"),
                owner=self._root_owner(root_id, "local"))
            id_by_key[node.key] = tid
            self.conn.execute(
                "UPDATE tasks SET plan_json=? WHERE id=?",
                (json.dumps({
                    "engine": "coordinator",
                    "node_key": node.key,
                    "subagent": node.subagent,
                    "node_kind": node.kind,
                    "request": node.request,
                    "branch": node.branch,
                    "parallel_group": node.parallel_group,
                }, ensure_ascii=False), tid))
        # 依赖
        for node in graph.nodes:
            tid = id_by_key[node.key]
            deps = [id_by_key[d] for d in node.depends_on if d in id_by_key]
            if deps:
                self.conn.execute("UPDATE tasks SET depends_on_json=? WHERE id=?",
                                  (json.dumps(deps, ensure_ascii=False), tid))
        self.conn.commit()
        db.update_task(self.conn, root_id, status=IN_PROGRESS,
                       plan={"engine": "coordinator", "domain": pack.id,
                             "risk": decision.risk, "nodes": list(id_by_key.values()),
                             "planning_source": "local_rules", "model_planning": False})
        events.emit(self.conn, events.TASK_STATUS, task_id=root_id, actor="coordinator",
                    detail={"status": IN_PROGRESS, "domain": pack.id,
                            "branches": graph.branches, "nodes": len(graph.nodes)})
        return {"task_id": root_id, "verdict": "pass", "domain": pack.id,
                "graph": graph.to_dict(), "route": decision.to_dict()}

    def _run_subagent_node(self, task, root) -> bool:
        started_at, started = db.now(), time.monotonic()
        meta = {"root_id": root.id, "task_id": task.id, "branch": task.plan.get("branch"),
                "subagent": task.plan.get("subagent"), "started_at": started_at}
        events.emit(self.conn, "subagent_started", task_id=task.id, actor="scheduler", detail=meta)
        try:
            return self._execute_subagent_node(task, root)
        except Exception as exc:
            return self._handle_node_failure(task, task.plan.get("subagent"),
                                             {"ok": False, "error": f"execution_exception:{type(exc).__name__}"}, root)
        finally:
            current = db.get_task(self.conn, task.id)
            events.emit(self.conn, "subagent_finished", task_id=task.id, actor="scheduler",
                        detail={**meta, "ended_at": db.now(),
                                "duration_ms": round((time.monotonic() - started) * 1000),
                                "status": current.status, "error": current.result.get("error")})

    def _execute_subagent_node(self, task, root) -> bool:
        """执行一个 Coordinator 子节点（固定能力 subagent）。返回是否处理了该任务。"""
        sub_id = task.plan.get("subagent")
        if not sub_id:
            return False
        # 汇总依赖节点的证据作为纯计算节点输入
        # 统一节点输出协议：计算型子节点优先消费"标准化结果"，无则回退原始 evidence。
        # 标准化记录带 entity_canonical/规范单位/证据 id/转换记录，保证核验/对比对齐并可追溯。
        dep_records: list = []
        for dep_id in task.depends_on:
            dep = db.get_task(self.conn, dep_id)
            if dep is None:
                continue
            dr = (dep.result or {})
            norm = dr.get("normalized") or []
            if norm:
                dep_records.extend(norm)
            else:
                dep_records.extend(dr.get("evidence", []) or [])
        req = self._build_sa_request(task.plan.get("request") or {})
        scenario = f"user:{self._root_ws(task.root_id, 'default')}:{root.slots.get('__domain__','')}"
        runner = SubagentRunner(self.runtime, cfg=self.cfg)
        res = runner.run(sub_id, req, task_id=task.id, root_id=task.root_id,
                         scenario=scenario, input_records=dep_records)
        out = res.to_dict()
        if res.needs_user:
            db.update_task(self.conn, task.id, status=WAITING_USER,
                           expected_statuses=(IN_PROGRESS,), require_active_root=True,
                           result={"subagent": sub_id, **out})
            return True
        if not res.ok:
            return self._handle_node_failure(task, sub_id, out, root)
        saved = db.update_task(self.conn, task.id, status=COMPLETED,
                       expected_statuses=(IN_PROGRESS,), require_active_root=True,
                       result={"subagent": sub_id, "branch": task.plan.get("branch"),
                               "evidence": res.evidence, "normalized": res.normalized,
                               "findings": res.findings, "limitations": res.limitations})
        if not saved:
            return False  # Cancellation or recovery won; discard the late result.
        events.emit(self.conn, events.TASK_STATUS, task_id=task.id,
                    actor=f"subagent:{sub_id}",
                    detail={"status": COMPLETED, "evidence": len(res.evidence),
                            "branch": task.plan.get("branch")})
        return True

    def _handle_node_failure(self, task, sub_id: str, out: dict, root) -> bool:
        """节点失败分流：可恢复瞬时错误 -> RETRYING（有界），其余 -> FAILED（分支可部分失败）。

        已完成工作与证据不丢：retry_count/last_error/recovery_steps 持久化在 plan/result；
        重试只重跑该节点，汇聚节点仍可使用其他成功分支证据。
        """
        max_retries = int((self.cfg or {}).get("limits", {}).get("max_node_retries", 2))
        plan = dict(task.plan or {})
        retry_count = int(plan.get("retry_count", 0))
        error = out.get("error")
        retriable = _is_retriable_error(error) and retry_count < max_retries
        recovery_steps = [
            f"重跑 subagent「{sub_id}」（不重放副作用，只读研究节点无外部写）",
            "若再次失败则继续计数，超过上限后该分支标记失败并保留其他分支结果",
        ]
        if retriable:
            retry_count += 1
            plan["retry_count"] = retry_count
            saved = db.update_task(self.conn, task.id, status=RETRYING, plan=plan,
                           expected_statuses=(IN_PROGRESS,), require_active_root=True,
                           result={"subagent": sub_id, **out,
                                   "retry_count": retry_count, "max_retries": max_retries,
                                   "last_error": error,
                                   "recovery_steps": recovery_steps})
            if not saved:
                return False
            events.emit(self.conn, events.RECOVERY, task_id=task.id,
                        actor=f"subagent:{sub_id}",
                        detail={"status": RETRYING, "error": error,
                                "retry_count": retry_count, "max_retries": max_retries,
                                "branch": task.plan.get("branch")})
        else:
            # 超上限或非瞬时：FAILED（并行分支允许部分失败，不伪装成功）
            exhausted = retry_count >= max_retries and _is_retriable_error(error)
            saved = db.update_task(self.conn, task.id, status=FAILED,
                           expected_statuses=(IN_PROGRESS,), require_active_root=True,
                           result={"subagent": sub_id, **out,
                                   "retry_count": retry_count,
                                   "gave_up_retrying": bool(exhausted)})
            if not saved:
                return False
            events.emit(self.conn, events.ERROR, task_id=task.id,
                        actor=f"subagent:{sub_id}",
                        detail={"error": error, "branch": task.plan.get("branch"),
                                "retry_count": retry_count, "exhausted": exhausted})
        return True

    @staticmethod
    def _build_sa_request(raw: dict):
        return sa_mod.SubagentRequest(
            query=raw.get("query", ""),
            target_sites=raw.get("target_sites", []) or [],
            allowed_domains=raw.get("allowed_domains", []) or [],
            required_fields=raw.get("required_fields", []) or [],
            time_range=raw.get("time_range"),
            max_sources=int(raw.get("max_sources", 8) or 8),
            allow_followup=bool(raw.get("allow_followup", False)),
            completion_criteria=raw.get("completion_criteria", ""),
            authorization=raw.get("authorization", ""),
            extra=raw.get("extra", {}) or {},
        )


    def _build_action(self, task, root) -> dict:
        """决定子任务下一步动作。模板 tool_args 优先（场景配置），否则交给 LLM 驱动。"""
        tool = task.plan.get("tool_hint")
        tool_args = task.plan.get("tool_args")
        if tool and tool_args:
            # 模板动作：场景配置的确定性动作，不经模型，标记来源便于审计分流。
            return {"type": llm.ACTION_TOOL_CALL, "tool": tool,
                    "args": _coerce_numbers(_interp(tool_args, task.slots, root.objective)),
                    "reason": task.objective, "_source": "template"}
        ctx = {
            "goal": root.objective,
            "tool_hint": tool,
            "available_tools": [tool] if tool else self.runtime.registry.names(),
            "tool_specs": [
                {"name": spec.name, "description": spec.description, "parameters": spec.params_schema}
                for name, spec in self.runtime.registry.all().items()
                if not tool or name == tool
            ],
        }
        return self.driver.next_action(task, ctx)

    def _run_subtask(self, task, root) -> bool:
        # 取消/终态守卫：根或自身已终结则不再执行（迟到 tick 不覆盖已取消状态）。
        if root.status in TERMINAL_STATUSES or task.status in TERMINAL_STATUSES:
            return False
        if task.status != IN_PROGRESS:
            # 原子领取：READY/PENDING -> IN_PROGRESS，避免并发 worker 重复领取。
            if not db.transition_status(self.conn, task.id, (PENDING, READY), IN_PROGRESS):
                return False  # 已被其他 worker 领取
            events.emit(self.conn, events.TASK_STATUS, task_id=task.id,
                        actor=f"agent:{task.agent_role}",
                        detail={"from": task.status, "to": IN_PROGRESS, "status": IN_PROGRESS})

        # Coordinator 动态图节点：直接走固定能力 Subagent（不经过 LLM 动作/旧模板）
        if task.plan.get("engine") == "coordinator":
            return self._run_subagent_node(task, root)
        via_template = bool(task.plan.get("tool_hint") and task.plan.get("tool_args"))
        action = self._build_action(task, root)
        if getattr(self.driver, "name", "local") in ("local", "base"):
            events.emit(self.conn, events.LLM_CALL, task_id=task.id,
                        actor=f"agent:{task.agent_role}",
                        detail={"action": action.get("type"), "tool": action.get("tool"),
                                "driver": "rule", "real_model_call": False,
                                "via": "template" if via_template else "local_rules"})
        else:
            self._record_driver_reports(task)  # 抽取真实 HTTP 调用（usage/降级/重试）
            events.emit(self.conn, events.LLM_CALL, task_id=task.id,
                        actor=f"agent:{task.agent_role}",
                        detail={"action": action.get("type"), "tool": action.get("tool"),
                                "driver": self.driver.name,
                                "model": getattr(self.driver, "model", None),
                                "real_model_call": True})
        try:
            if db.is_status(self.conn, task.root_id, CANCELLED) or \
               db.is_status(self.conn, task.id, CANCELLED, FAILED, COMPLETED):
                return False  # 执行期间被取消/已终结：丢弃迟到结果
            if action["type"] == llm.ACTION_TOOL_CALL and action.get("tool"):
                expected = task.plan.get("tool_hint")
                if expected and action["tool"] != expected:
                    raise RuntimeError(f"子任务工具范围不匹配：预期 {expected}，收到 {action['tool']}")
                result = self.runtime.call(
                    action["tool"], action.get("args", {}), task,
                    reason=action.get("reason", ""),
                )
                self._after_tool(task, action["tool"], result)
                return True
            if action["type"] in (llm.ACTION_ANSWER, llm.ACTION_RETRY):
                self._finish_task(task, root)
                return True
        except permissions.ApprovalRequired as exc:
            if db.is_status(self.conn, task.root_id, CANCELLED):
                return
            db.update_task(self.conn, task.id, status=WAITING_APPROVAL,
                           result={"pending_approval": exc.approval_id, "tool": exc.tool_name})
            return False  # 不阻塞其他分支
        except ToolResultInDoubt as exc:
            # 副作用工具结果未知（本地不可中断超时）：进入待人工确认，绝不自动重试。
            db.update_task(self.conn, task.id, status=WAITING_EVENT,
                           result={"in_doubt": True, "tool": exc.tool_name,
                                   "error": exc.reason, "code": "result_in_doubt",
                                   "message": "动作可能已执行但结果未确认，需人工核对后再决定是否重试"})
            events.emit(self.conn, events.TASK_STATUS, task_id=task.id, actor="runtime",
                        detail={"from": task.status, "to": WAITING_EVENT,
                                "status": WAITING_EVENT, "in_doubt": True, "tool": exc.tool_name})
            return False
        except permissions.PermissionDenied as exc:
            db.update_task(self.conn, task.id, status=FAILED,
                           result={"error": f"越权被阻止：{exc.reason}", "code": "permission_denied"})
            events.emit(self.conn, events.ERROR, task_id=task.id, actor="governance",
                        detail={"code": "permission_denied", "reason": exc.reason})
            return True
        except Exception as exc:  # noqa: BLE001 - 工具失败如实落账，不描述为成功
            db.update_task(self.conn, task.id, status=FAILED,
                           result={"error": f"执行失败：{str(exc)[:300]}", "code": "tool_failure"})
            events.emit(self.conn, events.ERROR, task_id=task.id, actor="runtime",
                        detail={"error": str(exc)[:300]})
            return True
        return False

    def _after_tool(self, task, tool: str, result: dict) -> None:
        # 终态守卫：工具返回时若根或本任务已取消/终结（取消恰发生在工具执行期间），
        # 不写 COMPLETED，避免迟到结果覆盖 CANCELLED。
        if db.is_status(self.conn, task.root_id, CANCELLED):
            return
        if db.is_status(self.conn, task.id, CANCELLED, FAILED):
            return
        if tool == "kb_search":
            chunks = result.get("chunks", [])
            # 检索员只产出证据；核验员（依赖检索员）负责起草与引用核验，避免答案重复
            if task.agent_role == ROLE_RESEARCHER:
                db.update_task(self.conn, task.id, status=COMPLETED,
                               result={"chunks": chunks, "evidence_count": result.get("count", 0)})
                events.emit(self.conn, events.TASK_STATUS, task_id=task.id, actor="scheduler",
                            detail={"status": COMPLETED, "evidence_count": len(chunks)})
            else:
                db.update_task(self.conn, task.id,
                               result={"chunks": chunks, "evidence_count": result.get("count", 0)})
                self._finish_task(task)
        elif tool == "geo_radius_search":
            db.update_task(self.conn, task.id,
                           result={"geo": result, "tool_result": result, "geo_tool": tool})
            if task.agent_role == ROLE_RESEARCHER:
                # 检索员产出候选即完成；核验员独立复核距离与证据
                db.update_task(self.conn, task.id, status=COMPLETED)
                events.emit(self.conn, events.TASK_STATUS, task_id=task.id, actor="scheduler",
                            detail={"status": COMPLETED, "geo_candidates": result.get("total", 0)})
            else:
                self._finish_task(task)
        elif tool.startswith("geo_"):
            db.update_task(self.conn, task.id, status=COMPLETED, result={"tool_result": result})
            events.emit(self.conn, events.TASK_STATUS, task_id=task.id, actor="scheduler",
                        detail={"status": COMPLETED, "tool": tool})
        else:
            # 副作用工具（下单/通知）或其他执行类工具
            db.update_task(self.conn, task.id, status=COMPLETED,
                           result={"tool_result": result, "tool": tool})
            events.emit(self.conn, events.TASK_STATUS, task_id=task.id, actor="scheduler",
                        detail={"status": COMPLETED, "tool": tool})

    # ---------- 汇总/起草/引用核验 ----------

    def _gather_evidence(self, task) -> tuple[list[dict], dict | None]:
        """收集同根任务中的知识 chunks 与 GEO 结果。"""
        chunks, geo = [], None
        for child in db.list_children(self.conn, task.root_id):
            if child.result.get("chunks"):
                chunks = child.result["chunks"]
            if child.result.get("geo"):
                geo = child.result["geo"]
        if task.result.get("chunks"):
            chunks = task.result["chunks"]
        if task.result.get("geo"):
            geo = task.result["geo"]
        return chunks, geo

    def _finish_task(self, task, root=None) -> None:
        root_task = root or db.get_task(self.conn, task.root_id)
        # 迟到的草稿/核验结果不得覆盖已取消或已终结的任务。
        if root_task.status in TERMINAL_STATUSES or task.status in TERMINAL_STATUSES:
            if root_task.status == CANCELLED or task.status == CANCELLED:
                return
        chunks, geo = self._gather_evidence(task)

        if geo is not None and task.agent_role == ROLE_VERIFIER:
            result = self._verify_geo(geo)
        elif geo is not None and task.agent_role == ROLE_RESEARCHER:
            result = {"geo": geo, "tool_result": geo}
        else:
            if getattr(self.driver, "name", "local") not in ("local", "base"):
                draft = self.driver.draft_answer(root_task.objective, chunks, task.slots)
                self._record_driver_reports(task)
            else:
                draft = self.driver.draft_answer(root_task.objective, chunks, task.slots)
            conflicts = self._detect_conflicts(chunks)
            answer_text = draft.get("text", "")
            has_factual = bool(answer_text.strip()) and not draft.get("no_evidence", not chunks)
            vres = verification.verify_answer(
                answer_text, draft.get("citations", []), chunks,
                has_factual_answer=has_factual,
            )
            vdict = vres.to_dict()
            result = {
                "answer": answer_text,
                "citations": [
                    {"chunk_id": c["chunk_id"], "doc_name": c["doc_name"], "section": c["section"],
                     "citations": c.get("citations", []), "snippet": c["text"][:200]}
                    for c in chunks if c["chunk_id"] in draft.get("citations", [])
                ],
                # 四档核验结论：verified / partial / insufficient / failed
                "verification": vdict,
                "citation_check": {"passed": vdict["passed"], "problems": vdict["problems"]},
                "conflicts": conflicts,
                "no_evidence": vres.verdict == verification.INSUFFICIENT
                              or draft.get("no_evidence", not chunks),
            }
        db.update_task(self.conn, task.id, status=COMPLETED, result=result)
        events.emit(self.conn, events.TASK_STATUS, task_id=task.id, actor="scheduler",
                    detail={"status": COMPLETED,
                            "citations": len(result.get("citations", [])),
                            "conflicts": len(result.get("conflicts", []))})

    def _verify_geo(self, geo: dict) -> dict:
        """GEO 核验：独立复算距离、检查坐标系/来源，证据链逐条可查。"""
        center = geo.get("center") or {}
        radius = float(geo.get("radius_km", 0) or 0)
        problems = []
        citations = []
        lines = []
        c_lat, c_lon = center.get("lat"), center.get("lon")
        for cand in geo.get("candidates", []):
            # 独立复算 haversine（核验员不采信工具自报距离）
            recomputed = None
            if c_lat is not None:
                recomputed = haversine_km(c_lat, c_lon, cand["lat"], cand["lon"])
                if recomputed > radius + 0.05:
                    problems.append({"candidate": cand["name"],
                                     "problem": f"复算距离 {recomputed:.2f}km 超出半径 {radius}km"})
            dist_note = f"{cand['distance_km']}km"
            if recomputed is not None:
                dist_note += f"（独立复算 {recomputed:.2f}km）"
            props = cand.get("properties", {})
            line = (
                f"- {cand['name']}：距中心 {dist_note}，类别 {props.get('category', '未知')}"
                + (f"，评分 {props.get('rating')}" if props.get("rating") is not None else "")
            )
            lines.append(line)
            citations.append({
                "chunk_id": f"geo:{cand['name']}",
                "doc_name": f"GEO 候选 · {cand['name']}",
                "section": cand.get("source", "unknown"),
                "citations": cand.get("evidence", []),
                "snippet": "；".join(cand.get("evidence", [])),
            })

        crs_ok = geo.get("crs") == "EPSG:4326"
        if not crs_ok:
            problems.append({"problem": f"坐标系异常: {geo.get('crs')}"})
        source = geo.get("source", "unknown")
        source_label = {"local_geojson": "本地样例数据（GeoJSON）", "mock": "模拟数据"}.get(source, source)

        if geo.get("supported") is False:
            answer = f"空间服务返回不支持：{geo.get('reason', 'unknown')}；未编造结果。"
        elif geo.get("total", 0) == 0:
            answer = f"在半径 {radius}km 内未找到符合条件的候选地点（数据来源：{source_label}）。"
        else:
            header = f"找到 {geo['total']} 个候选地点（按距离排序，{source_label}，坐标系 EPSG:4326）："
            footer = ("注：候选为样例/检索地点数据；直线距离为 haversine 球面距离，已独立复算；"
                      "道路路线/通勤时间由前端地图插件按其能力提供，本结论不含道路距离；"
                      "评分、营业、安静等属性若无证据则标注为未核验。")
            answer = "\n".join([header] + lines + [footer])

        return {
            "answer": answer,
            "geo": geo,
            "citations": citations,
            "citation_check": {"passed": not problems, "problems": problems},
            "conflicts": [],
            "no_evidence": geo.get("total", 0) == 0 and geo.get("supported") is not False,
            "geo_source": source,
        }

    @staticmethod
    def _detect_conflicts(chunks: list[dict]) -> list[dict]:
        """检测同时有效的不同资料对同一指标给出冲突数值。

        已过期/已失效的版本（expires_at 早于今天）已在检索阶段排除、不参与回答，
        因此不再与现行版本构成"冲突"；只比较当前有效资料之间的矛盾。
        """
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        num_re = re.compile(r"(\d+(?:\.\d+)?)\s*(元|块|k|km|公里|%)?")
        by_doc: dict[str, list] = {}
        for ch in chunks:
            exp = ch.get("expires_at")
            if exp and exp < today:
                continue  # 已失效版本不参与冲突判定
            by_doc.setdefault(ch["doc_name"], []).append(ch)
        groups: dict[str, list[str]] = {}
        for name in by_doc:
            stem = re.sub(r"[_\-]?20\d{2}", "", name)
            groups.setdefault(stem, []).append(name)
        conflicts = []
        for stem, names in groups.items():
            if len(names) <= 1:
                continue
            vals = {}
            for name in names:
                text = " ".join(c["text"] for c in by_doc[name])
                amounts = {float(v) for v, u in num_re.findall(text) if u in ("元", "块")}
                vals[name] = amounts
            all_vals = [v for s in vals.values() for v in s]
            if len(set(all_vals)) > 1:
                conflicts.append({
                    "topic": stem,
                    "sources": names,
                    "values": {k: sorted(v) for k, v in vals.items()},
                    "message": "不同版本资料给出的数值不一致，需人工确认适用版本",
                })
        return conflicts

    # ---------- 出口闸门 ----------

    def _gate_exit_coordinator(self, root, children) -> None:
        """Coordinator 研究图出口：只读研究闭环。

        - 汇总各分支证据（已确认/推断/未确认/无法回答）与冲突；
        - 研究节点部分失败 -> partially_completed（不伪装成功，也不丢弃其他分支）；
        - 有未决高风险审批 -> 不通过（只读研究通常无高风险动作）。
        """
        from . import evidence as E
        all_records: list = []
        failures, branches_ok, branches_failed = [], set(), set()
        conflicts = []
        layer_counts = {"confirmed": 0, "inferred": 0, "unconfirmed": 0, "unanswerable": 0}
        comparison = None
        for c in children:
            r = c.result or {}
            node = c.plan or {}
            br = node.get("branch")
            if c.status == FAILED or r.get("ok") is False:
                failures.append({"task": c.title, "subagent": r.get("subagent"),
                                 "branch": br, "error": r.get("error")})
                if br:
                    branches_failed.add(br)
                continue
            if br:
                branches_ok.add(br)
            recs = r.get("evidence", []) or []
            all_records.extend(recs)
            findings = r.get("findings", {}) or {}
            if findings.get("conflicts"):
                conflicts.extend(findings["conflicts"])
            if node.get("subagent") == sa_mod.SA_COMPARISON:
                comparison = findings.get("comparison")
        records = []
        for d in all_records:
            try:
                records.append(E.EvidenceRecord.from_dict(d))
            except Exception:  # noqa: BLE001
                pass
        layers = E.answer_layers(records)
        for k in layer_counts:
            layer_counts[k] = layers["counts"].get(k, 0)
        if layers["conflicts"]:
            for cf in layers["conflicts"]:
                conflicts.append(cf)
        # 未决高风险审批（只读研究不应有，防御性检查）
        pending_high = self.conn.execute(
            """SELECT COUNT(*) AS n FROM approvals WHERE status='pending'
               AND task_id IN (SELECT id FROM tasks WHERE root_id=?)""",
            (root.id,)).fetchone()["n"]
        verified = (not failures) and (not conflicts) and (pending_high == 0) \
            and layer_counts["confirmed"] > 0
        # 无任何已确认事实（全部来源失败/未召回/证据不足）：不假装"无法确认即终态"，
        # 而是**暂停并请用户选择如何继续**（用户确认优先，避免默认值掩盖不确定性）。
        no_confirmed = layer_counts["confirmed"] == 0
        required = {"normalize", "verify", "compare"}
        finished = {c.plan.get("node_kind") for c in children if c.status == COMPLETED}
        verified = verified and required.issubset(finished) and comparison is not None
        status = COMPLETED if verified else PARTIALLY_COMPLETED
        if no_confirmed:
            status = PARTIALLY_COMPLETED
        result = {
            "engine": "coordinator",
            "domain": (root.plan or {}).get("domain"),
            "verified": verified,
            "verified_partial": bool(failures) and layer_counts["confirmed"] > 0,
            "status_bucket": "verified" if verified else ("partial" if failures else "insufficient"),
            "layers": layer_counts,
            "branches_ok": sorted(branches_ok),
            "branches_failed": sorted(branches_failed),
            "failures": failures,
            "conflicts": _dedup_conflicts(conflicts),
            "comparison": comparison,
            "evidence_count": len(records),
            "pending_approvals": pending_high,
            "answer": _compose_coordinator_answer(root, layer_counts, failures,
                                                  sorted(branches_ok), sorted(branches_failed)),
        }
        # 困难/部分完成：持久化暂停快照（已完成工作/证据/失败原因/待决问题/恢复步骤）
        if failures or conflicts or layer_counts["confirmed"] == 0:
            result["pause_snapshot"] = self._build_pause_snapshot(
                root, children, records, failures, _dedup_conflicts(conflicts),
                sorted(branches_ok), sorted(branches_failed))
        # 无已确认事实 -> 结构化确认（补充来源/只给有据部分/用户提供资料/取消）
        if no_confirmed:
            opts = [
                confirm_mod.ConfirmationOption(
                    "broaden", "放宽条件并补充允许的来源域名后重试",
                    impact="展开更多外部检索分支；不编造，可能仍需您配置允许域名", risk="low"),
                confirm_mod.ConfirmationOption(
                    "state_gap", "只列出有据部分，明确标注哪些无法确认",
                    impact="如实交付已核实内容，把缺口显式列出，不做结论推断", risk="low"),
                confirm_mod.ConfirmationOption(
                    "provide", "我来提供或切到已导入知识/文件",
                    impact="导入/激活资料后再重试，仅检索已导入知识", risk="low"),
                confirm_mod.ConfirmationOption(
                    "cancel", "就此结束并如实说明无证据",
                    impact="把任务标记为已结束（结果标注‘无证据/未核实’）", risk="low"),
            ]
            result["confirmation"] = confirm_mod.build_confirmation(
                confirm_mod.PAUSE_NO_EVIDENCE,
                confirmed_info=[f"已回收 {len(records)} 条证据记录",
                                f"成功分支: {sorted(branches_ok) or '无'}",
                                f"失败分支: {sorted(branches_failed) or '无'}"],
                problem="所有研究分支均未产出可确认为事实的证据，无法安全给出结论。",
                decision_needed="证据不足时应如何继续？",
                question="没有足够的已确认证据，您希望怎么继续？",
                options=opts).to_dict()
            result["message"] = result["confirmation"]["question"]
            result["options"] = [o["label"] for o in result["confirmation"]["options"]]
            # 保持 partially_completed + 明确待办（等待用户继续），不当作最终交付
            if not db.update_task(self.conn, root.id, status=status, result=result,
                                  expected_statuses=(IN_PROGRESS,)):
                return
            events.emit(self.conn, events.CLARIFY, task_id=root.id, actor="coordinator",
                        detail={"reason_code": confirm_mod.PAUSE_NO_EVIDENCE,
                                "branches_failed": sorted(branches_failed),
                                "evidence": len(records)})
            self._record_coordinator_telemetry(root, result, children, records)
            return
        if not db.update_task(self.conn, root.id, status=status, result=result,
                              expected_statuses=(IN_PROGRESS,)):
            return
        events.emit(self.conn, events.TASK_STATUS, task_id=root.id, actor="coordinator",
                    detail={"status": status, "verified": verified,
                            "partial": bool(failures), "evidence": len(records)})
        self._record_coordinator_telemetry(root, result, children, records)

    def _build_pause_snapshot(self, root, children, records, failures, conflicts,
                              branches_ok, branches_failed) -> dict:
        """构造困难暂停快照（部分完成/证据不足/冲突时随结果持久化，可跨重启查看与恢复）。"""
        completed_work = []
        evidence_refs = []
        for c in children:
            if c.status != COMPLETED:
                continue
            completed_work.append({"task": c.title,
                                   "subagent": (c.plan or {}).get("subagent"),
                                   "branch": (c.plan or {}).get("branch")})
        for rec in records[:50]:
            evidence_refs.append({
                "entity": getattr(rec, "entity", ""), "field": getattr(rec, "field", ""),
                "source_type": getattr(rec, "source_type", ""),
                "url": getattr(rec, "source_url", ""),
                "title": getattr(rec, "source_title", ""),
                "version": getattr(rec, "source_version", ""),
                "collected_at": getattr(rec, "collected_at", "")})
        open_questions = []
        if branches_failed:
            open_questions.append(
                f"分支 {', '.join(branches_failed)} 的外部来源失败/未配置，是否配置允许域名后重试，或仅基于现有分支给结论？")
        if conflicts:
            open_questions.append("存在来源冲突，需要人工确认适用版本/取舍。")
        if not records:
            open_questions.append("没有任何可确认证据：请补充文件/允许域名，或放宽检索条件。")
        recovery_steps = []
        if branches_failed:
            recovery_steps += [
                "1) 在领域配置中为失败来源配置 allowed_domains/search_endpoint（只读）",
                "2) 重新发起任务或对失败分支重试（研究节点幂等、无外部写）",
            ]
        if conflicts:
            recovery_steps.append("3) 由用户选择适用版本或要求并列展示冲突")
        if not recovery_steps:
            recovery_steps = ["补充资料后重新发起；系统不会在无证据时编造结论"]
        return {
            "paused_at": db.now(),
            "domain": (root.plan or {}).get("domain"),
            "completed_work": completed_work,
            "evidence_refs": evidence_refs,
            "failures": failures,
            "conflicts": conflicts,
            "open_questions": open_questions,
            "recovery_steps": recovery_steps,
        }

    def _record_coordinator_telemetry(self, root, result, children, records) -> None:
        """任务结束轨迹沉淀（只读研究闭环）。仅记录，不据此自动改任何生产配置。"""
        try:
            sub_ids = sorted({(c.plan or {}).get("subagent") for c in children
                              if (c.plan or {}).get("subagent")})
            sources = sorted({(rec.source_type or "") for rec in records})
            tool_rows = self.conn.execute(
                """SELECT json_extract(detail_json,'$.tool') AS tool,
                          json_extract(detail_json,'$.ok') AS ok
                   FROM events WHERE kind='tool_result'
                   AND task_id IN (SELECT id FROM tasks WHERE root_id=?)""",
                (root.id,)).fetchall()
            tool_calls = [r["tool"] for r in tool_rows if r["tool"]]
            failures = result.get("failures", [])
            attributions = impr.attribute_failure(
                tool_failures=failures or None,
                source_errors=failures or None,
                verdict=result.get("status_bucket"))
            impr.record_run(
                self.conn, goal=root.objective, final_status=root.status,
                route={"task_id": root.id, "domain": (root.plan or {}).get("domain"),
                       "engine": "coordinator"},
                domains=[(root.plan or {}).get("domain")], subagents=sub_ids,
                sources=sources, evidence_count=result.get("evidence_count", 0),
                tool_calls=tool_calls, tool_failures=failures,
                decision_ready=bool(result.get("verified")),
                attributions=attributions)
        except Exception:  # noqa: BLE001 - 观测失败不影响主流程
            pass

    def _gate_exit(self, root) -> None:
        children = db.list_children(self.conn, root.id)
        findings = {"passed": True, "problems": [], "verified_partial": False}

        answers, citations_all, conflicts_all = [], [], []
        tool_failures, denied = [], []
        geo_payload = None

        for c in children:
            r = c.result
            if r.get("answer"):
                answers.append(r["answer"])
                citations_all.extend(r.get("citations", []))
            v = r.get("verification")
            if v:
                verdict = v.get("verdict")
                if verdict == "failed":
                    findings["passed"] = False
                    findings["problems"].append(
                        {"task": c.title, "kind": "verification_failed",
                         "detail": v.get("problems")})
                elif verdict in ("partial", "insufficient"):
                    findings["verified_partial"] = True
                    if verdict == "insufficient":
                        findings["problems"].append(
                            {"task": c.title, "kind": "evidence_insufficient",
                             "detail": v.get("problems") or "证据不足以核验答案"})
            elif r.get("citation_check") and not r["citation_check"]["passed"]:
                findings["passed"] = False
                findings["problems"].append(
                    {"task": c.title, "kind": "citation", "detail": r["citation_check"]["problems"]})
            if r.get("conflicts"):
                conflicts_all.extend(r["conflicts"])
            if r.get("no_evidence"):
                findings["verified_partial"] = True
            if r.get("geo"):
                geo_payload = r["geo"]
            if r.get("code") == "permission_denied":
                denied.append({"task": c.title, "error": r.get("error")})
            if c.status == FAILED:
                tool_failures.append({"task": c.title, "error": r.get("error", "执行失败"),
                                      "code": r.get("code")})

        # 已发生的副作用（含幂等重放），从审计事件汇总
        side_effects = []
        se_rows = self.conn.execute(
            """SELECT detail_json FROM events WHERE kind='tool_result'
               AND json_extract(detail_json,'$.idempotency_key') IS NOT NULL
               AND json_extract(detail_json,'$.ok')=1
               AND task_id IN (SELECT id FROM tasks WHERE root_id=?)""",
            (root.id,),
        ).fetchall()
        for r in se_rows:
            d = json.loads(r["detail_json"])
            side_effects.append({"tool": d.get("tool"), "result": d.get("result"),
                                 "replay": bool(d.get("idempotent_replay"))})

        # 规则 1：高风险（risk=high）副作用动作必须有匹配的审批记录（按工具元数据，不硬编码工具名）
        high_risk_tools = {
            name for name, spec in self.runtime.registry.all().items()
            if getattr(spec, "risk", "") == "high"
        }
        high_risk_done = self.conn.execute(
            """SELECT COUNT(*) AS n FROM events WHERE kind='tool_result'
               AND json_extract(detail_json,'$.ok')=1
               AND json_extract(detail_json,'$.tool') IN (%s)
               AND task_id IN (SELECT id FROM tasks WHERE root_id=?)""" %
            ",".join("?" * len(high_risk_tools)) if high_risk_tools else
            """SELECT 0 AS n FROM tasks WHERE id=?""",
            (*sorted(high_risk_tools), root.id) if high_risk_tools else (root.id,),
        ).fetchone()["n"]
        # 每个已完成的高风险副作用动作，都必须有"同工具 + 同参数哈希 + 同 root"的 approved 审批；
        # 不能用"root 下存在任意 approved"代替"该高风险调用存在匹配审批"。
        high_risk_events = self.conn.execute(
            """SELECT json_extract(detail_json,'$.tool') AS tool,
                      json_extract(detail_json,'$.args') AS args_json
               FROM events WHERE kind='tool_result'
               AND json_extract(detail_json,'$.ok')=1
               AND json_extract(detail_json,'$.tool') IN (%s)
               AND task_id IN (SELECT id FROM tasks WHERE root_id=?)""" %
            ",".join("?" * len(high_risk_tools)) if high_risk_tools else
            """SELECT NULL AS tool, NULL AS args_json FROM tasks WHERE id=? AND 0""",
            (*sorted(high_risk_tools), root.id) if high_risk_tools else (root.id,),
        ).fetchall()
        missing_approval = []
        for ev in high_risk_events:
            tool_name = ev["tool"]
            try:
                ev_args = json.loads(ev["args_json"] or "{}")
            except Exception:
                ev_args = {}
            ah = permissions.args_hash(tool_name, ev_args) if tool_name else None
            match = self.conn.execute(
                """SELECT a.id FROM approvals a JOIN tasks t ON t.id=a.task_id
                   WHERE a.tool_name=? AND a.status='approved' AND a.args_hash=?
                     AND t.root_id=?""",
                (tool_name, ah, root.id),
            ).fetchone() if ah else None
            if not match:
                missing_approval.append(tool_name)
        if high_risk_done and missing_approval:
            findings["passed"] = False
            findings["problems"].append(
                {"kind": "governance",
                 "detail": f"高风险动作缺少匹配审批（同工具+同参数+同root）: {sorted(set(missing_approval))}"})

        if tool_failures:
            findings["passed"] = False
            findings["problems"].append({"kind": "tool_failure", "detail": tool_failures})
        if denied:
            findings["passed"] = False
            findings["problems"].append({"kind": "permission_denied", "detail": denied})
        if conflicts_all:
            findings["passed"] = False
            findings["problems"].append({"kind": "source_conflict", "detail": conflicts_all})

        answer_text = "\n\n".join(answers)
        pending = self.conn.execute(
            """SELECT id, tool_name, args_json, reason FROM approvals WHERE status='pending'
               AND task_id IN (SELECT id FROM tasks WHERE root_id=?)""",
            (root.id,),
        ).fetchall()
        pending_approvals = [
            {"approval_id": r["id"], "tool": r["tool_name"],
             "args": json.loads(r["args_json"]), "reason": r["reason"]}
            for r in pending
        ]

        # 收集所有子任务的逐案核验档（verification.verdict），做总体严格判定
        verification_dicts = []
        for c in children:
            v = (c.result or {}).get("verification")
            if isinstance(v, dict):
                verification_dicts.append(v)
        overall = verification.overall_verified(
            verification_dicts=verification_dicts,
            conflicts=conflicts_all, tool_failures=tool_failures, denied=denied,
            pending_high_risk_approvals=len(pending_approvals) if False else 0,
            has_substantive_answer=bool(answer_text))
        # 高风险动作缺少匹配审批 或 存在待决高风险审批 -> 一定不通过
        gov_fail = any(pr.get("kind") == "governance" for pr in findings["problems"])
        strictly_verified = overall["verified"] and findings["passed"] and not gov_fail \
            and not pending_approvals
        result = {
            "answer": answer_text,
            "verified": strictly_verified,
            "verified_partial": overall["bucket"] == "partial" or findings["verified_partial"],
            "verification_bucket": overall["bucket"],
            "verification_counts": overall["counts"],
            "verification_reasons": overall["reasons"],
            "citations": citations_all,
            "conflicts": conflicts_all,
            "side_effects": side_effects,
            "tool_failures": tool_failures,
            "denied": denied,
            "pending_approvals": pending_approvals,
            "gate_problems": findings["problems"],
            "geo": geo_payload,
        }
        if findings["verified_partial"] and not answer_text:
            result["answer"] = "已检索授权知识库，但未找到足以支撑结论的依据，**无法确认**。请补充资料或明确信息来源。"
        if tool_failures and not answer_text:
            result["answer"] = "任务执行中工具失败，未达成目标，详见动作记录；已发生的动作不可撤回。"

        # 有答案但未完全核验：仍交付已验证部分，verified=false 并标注问题；
        # 无答案且失败/越权 → failed；纯无证据 → completed（明确无法确认也是可信交付）。
        if answer_text:
            status = COMPLETED
        elif tool_failures or denied:
            status = FAILED
        else:
            status = COMPLETED
        db.set_task_result_full(self.conn, root.id, result)
        db.update_task(self.conn, root.id, status=status)
        self._record_legacy_telemetry(root, result, children, status)
        events.emit(self.conn, events.GATE_EXIT, task_id=root.id, actor="verifier",
                    detail={"passed": strictly_verified, "bucket": overall["bucket"],
                            "problems": findings["problems"],
                            "verified_partial": result["verified_partial"],
                            "pending_approvals": len(pending_approvals)})

    def _record_legacy_telemetry(self, root, result, children, status) -> None:
        try:
            attributions = impr.attribute_failure(
                tool_failures=result.get("tool_failures") or None,
                permission_issues=result.get("denied") or None,
                source_errors=None,
                verdict=result.get("verification_bucket"),
                chunks=[] )
            impr.record_run(
                self.conn, goal=root.objective, final_status=status,
                route={"task_id": root.id, "engine": "scenario_template",
                       "scenario": root.slots.get("__scenario__")},
                subagents=[], sources=["document"],
                evidence_count=len(result.get("citations", [])),
                tool_calls=[], tool_failures=result.get("tool_failures", []),
                decision_ready=bool(result.get("verified")),
                attributions=attributions)
        except Exception:  # noqa: BLE001
            pass

    # ---------- 用户消息/澄清/取消/审批 ----------

    def retry_branch(self, root_id: str, branch: str | None = None) -> dict:
        with _root_lock(self.cfg.get("db_path", ""), root_id):
            return self._retry_branch_locked(root_id, branch)

    def _retry_branch_locked(self, root_id: str, branch: str | None = None) -> dict:
        """用户触发：仅重跑失败的只读研究分支，保留其余分支证据，并重算汇聚节点。

        - 只对 coordinator 只读研究节点（node_kind=research）且处于 FAILED/RETRYING 者重置为 READY；
        - 目标分支外的研究节点与证据原样保留；
        - 把可能带旧结果的汇聚节点(normalize/verify/compare)重置为 PENDING，
          待被重试研究(与已有分支)完成后自动 READY 并**重算汇总**；
        - 返回每个失败分支的失败原因（区分 source_not_configured / source_unreachable / search_unreachable 等）。
        """
        root = db.get_task(self.conn, root_id)
        if root is None:
            raise KeyError("任务不存在")
        if root.status == CANCELLED:
            return {"error": "task_cancelled", "retried": []}
        if (root.plan or {}).get("engine") != "coordinator":
            return {"error": "only_coordinator", "retried": [], "recompute": [],
                    "reasons": {}}
        children = db.list_children(self.conn, root_id)
        if any(c.status == IN_PROGRESS for c in children):
            return {"error": "task_running", "retried": []}
        agg_kinds = {"normalize", "verify", "compare"}
        retried = []
        reasons = {}
        any_reset = False
        for c in children:
            cplan = c.plan or {}
            if cplan.get("node_kind") != "research":
                continue
            cb = cplan.get("branch")
            if branch and branch not in (cb, c.id):
                continue
            if c.status not in (FAILED, RETRYING):
                continue
            result = c.result or {}
            reasons.setdefault(cb or "main", result.get("error") or "unknown")
            plan = dict(cplan)
            plan["retry_count"] = 0
            self.conn.execute(
                "UPDATE tasks SET status=?, result_json=?, plan_json=?, updated_at=? WHERE id=?",
                (READY, "{}", json.dumps(plan, ensure_ascii=False), db.now(), c.id))
            retried.append({"branch": cb or "main", "task_id": c.id})
            any_reset = True
        if not any_reset:
            return {"retried": [], "recompute": [], "reasons": reasons}
        recompute = []
        for c in children:
            cplan = c.plan or {}
            if cplan.get("node_kind") in agg_kinds and                c.status in (COMPLETED, FAILED, PARTIALLY_COMPLETED):
                self.conn.execute(
                    "UPDATE tasks SET status=?, result_json=?, updated_at=? WHERE id=?",
                    (PENDING, "{}", db.now(), c.id))
                recompute.append({"kind": cplan.get("node_kind"), "task_id": c.id})
        # Previous conclusions are stale until aggregation and verification run again.
        keep = {k: v for k, v in root.result.items() if k in ("route", "pause_snapshot")}
        keep.update({"verified": False, "status_bucket": "retrying"})
        self.conn.execute("UPDATE tasks SET status=?, result_json=?, updated_at=? WHERE id=?",
                          (IN_PROGRESS, json.dumps(keep, ensure_ascii=False), db.now(), root_id))
        self.conn.commit()
        events.emit(self.conn, events.RECOVERY, task_id=root_id, actor="user_retry",
                    detail={"retried": retried, "recompute": recompute,
                            "branch": branch or "all", "reasons": reasons})
        return {"retried": retried, "recompute": recompute, "reasons": reasons}


    def user_message(self, root_id: str, text: str, slot_updates: dict | None = None) -> dict:
        root = db.get_task(self.conn, root_id)
        if root is None:
            raise FileNotFoundError(root_id)
        events.emit(self.conn, events.MESSAGE, task_id=root_id, actor="user",
                    detail={"text": text, "slots": slot_updates or {}})
        slots = dict(root.slots)
        # 上一轮存在槽位冲突待决：本轮文本是对冲突的选择
        pending_conflict = (root.result or {}).get("slot_conflict_pending")
        if pending_conflict:
            return self._resolve_slot_conflict(root, text, pending_conflict)

        # 先基于"尚未写入的原始已确认槽位"做冲突预检（必须在通用抽取/落库之前），
        # 否则旧值已被覆盖就检测不到冲突。
        route_info0 = root.result.get("route") or {}
        domain_id0 = slots.get("__domain__") or (route_info0.get("domains") or [{}])[0].get("id")
        if domain_id0 and root.status in (WAITING_USER, WAITING_EVENT, WAITING_EXTERNAL):
            incoming = self._detected_domain_slot_updates(domain_id0, text, slots, slot_updates)
            conflicts = self._slot_conflicts(root, incoming)
            if conflicts:
                return self._pause_for_slot_conflict(root_id, root, slots, conflicts, text)

        if slot_updates:
            slots.update(slot_updates)
        else:
            try:
                scenario_cfg = cfg_mod.load_scenario(slots.get("__scenario__", ""))
                if hasattr(self.driver, "extract_slots"):
                    slots.update(self.driver.extract_slots(text, scenario_cfg.get("slots", [])))
            except FileNotFoundError:
                pass
        slots["__last_message__"] = text
        db.update_task(self.conn, root_id, slots=slots)

        # 多轮语义分类并记录（缺失信息/约束变更/追问/新任务）；约束变更才触发重新核验/重规划。
        category = classify_followup(
            text, is_waiting_clarify=(root.status in (WAITING_EVENT, WAITING_USER) and bool(root.result.get("clarify"))),
            slot_updates=slot_updates)
        events.emit(self.conn, events.MESSAGE, task_id=root_id, actor="controller",
                    detail={"followup_class": category, "text": text[:80]})

        # 领域驱动（Coordinator）澄清恢复
        route_info = root.result.get("route") or {}
        domain_id = slots.get("__domain__") or (route_info.get("domains") or [{}])[0].get("id")
        if domain_id:
            slots = self._absorb_domain_slots(domain_id, text, slots, slot_updates)
            db.update_task(self.conn, root_id, slots=slots)
        if (root.status in (WAITING_EVENT, WAITING_USER)
                and (root.result.get("route") or root.result.get("wait_kind"))
                and domain_id):
            reg = dom_mod.load_default_registry()
            pack = reg.get(domain_id)
            decision = None
            if pack is not None:
                from .router import RouterAgent
                decision = RouterAgent(reg).route(
                    root.objective,
                    context={"domain": domain_id, "known_slots": slots})
                missing = [m for m in decision.missing_slots if not slots.get(m["name"])]
                # 若澄清问题里的槽位（如 energy_type）已在本轮回答中给出
                if not missing and root.result.get("clarify"):
                    missing = [m for m in root.result["clarify"] if not slots.get(m["name"])]
                if missing:
                    return {"status": WAITING_USER,
                            "still_missing": [m["name"] for m in missing]}
                db.set_task_result_full(self.conn, root_id, {})
                mat = self._materialize_domain(
                    root_id, root.objective, decision or _SimpleRoute(pack, slots), reg)
                if mat.get("verdict") == "pass":
                    return self.tick(root_id)
                return {"status": WAITING_USER, "verdict": mat.get("verdict")}

        if root.status in (WAITING_EVENT, WAITING_USER) and root.result.get("clarify"):
            still_missing = [m for m in root.result["clarify"] if not slots.get(m["name"])]
            if still_missing:
                return {"status": WAITING_USER,
                        "still_missing": [m["name"] for m in still_missing]}
            scenario_cfg = cfg_mod.load_scenario(slots.get("__scenario__", ""))
            gate = self._gate_entry(root_id, root.objective, scenario_cfg, slots)
            db.set_task_result_full(self.conn, root_id, {})
            if gate["verdict"] == "pass":
                self._materialize_plan(root_id, gate["plan"])
        return self.tick(root_id)

    @staticmethod
    def _user_slots(slots: dict) -> dict:
        """排除内部 __ 键与易变消息痕迹，只比较业务槽位。"""
        return {k: v for k, v in (slots or {}).items()
                if not str(k).startswith("__") and v not in (None, "", [])}

    def _resolve_slot_conflict(self, root, text: str, pending: dict) -> dict:
        raw_text = pending.get("raw_text", "")
        low = (text or "").strip()
        keep_old = any(k in low for k in ("保持", "原来", "沿用", "不改", "维持"))
        cancel = any(k in low for k in ("取消", "暂停", "先不", "算了"))
        domain_id = root.slots.get("__domain__")
        if cancel:
            return {"status": WAITING_USER, "cancelled_conflict": True}
        slots = dict(root.slots)
        if not keep_old and domain_id:
            # 采用新值：强制按引发冲突的那轮输入覆盖（金额/枚举等）
            try:
                ents = RouterAgent_extract(raw_text)
                money = ents.get("money") if ents.get("money") is not None else ents.get("budget")
                if money is not None:
                    slots["budget"] = money
            except Exception:  # noqa: BLE001
                pass
            absorbed = self._absorb_domain_slots(domain_id, raw_text, {}, None)
            for k, v in self._user_slots(absorbed).items():
                slots[k] = v
        # 无论哪种选择，都清除 pending 标记
        result = dict(root.result or {})
        result.pop("slot_conflict_pending", None)
        db.set_task_result_full(self.conn, root.id, result)
        db.update_task(self.conn, root.id, slots=slots)
        events.emit(self.conn, events.MESSAGE, task_id=root.id, actor="controller",
                    detail={"slot_conflict_resolved": "keep_old" if keep_old else "use_new"})
        # 继续正常澄清恢复流程
        return self.user_message(root.id, "__continue__", None) if False else \
            self._continue_after_clarify(root, slots)

    def _continue_after_clarify(self, root, slots) -> dict:
        """冲突解决后复用既有澄清恢复路径。"""
        domain_id = slots.get("__domain__") or \
            ((root.result or {}).get("route", {}).get("domains") or [{}])[0].get("id")
        if not domain_id:
            return self.tick(root.id)
        reg = dom_mod.load_default_registry()
        pack = reg.get(domain_id)
        decision = None
        if pack is not None:
            from .router import RouterAgent
            decision = RouterAgent(reg).route(
                root.objective, context={"domain": domain_id, "known_slots": slots})
            missing = [m for m in decision.missing_slots if not slots.get(m["name"])]
            if missing:
                return {"status": WAITING_USER, "still_missing": [m["name"] for m in missing]}
        db.set_task_result_full(self.conn, root.id, {})
        mat = self._materialize_domain(root.id, root.objective,
                                       decision or _SimpleRoute(pack, slots), reg)
        if mat.get("verdict") == "pass":
            return self.tick(root.id)
        return {"status": WAITING_USER, "verdict": mat.get("verdict")}

    def _slot_conflicts(self, root, incoming: dict) -> list[dict]:
        """用户新值与已确认业务槽位不一致 -> 冲突（不静默覆盖）。"""
        current = self._user_slots(root.slots)
        # 仅对当前处于等待澄清/确认的任务检查（运行中改约束走 followup 另议）
        if root.status not in (WAITING_USER, WAITING_EVENT, WAITING_EXTERNAL):
            return []
        return slots_mod.detect_slot_conflicts([], current, incoming)

    def _detected_domain_slot_updates(self, domain_id: str, text: str, slots: dict,
                                      slot_updates: dict | None) -> dict:
        """计算本轮文本显式表达的业务槽位新值（用于冲突预检，不改状态）。

        即使槽位已存在，用户在本轮显式改值（如"预算改成5万"）也要作为 incoming
        参与冲突检测，不被"已存在不覆盖"吞掉。
        """
        incoming: dict = {}
        if slot_updates:
            incoming.update({k: v for k, v in slot_updates.items()
                             if not str(k).startswith("__")})
        # 只有出现"改成/换成/改为/调整为/其实是/应该是"等改义信号时，才把本轮金额
        # 视为对既有预算的修改；普通首次补充（"预算5万纯电"）不算冲突。
        if re.search(r"改成|换成|改为|调整为|变更为|其实是|应该是|变到|调到", text or ""):
            try:
                ents = RouterAgent_extract(text)
                if (ents.get("money") if ents.get("money") is not None else ents.get("budget")) is not None:
                    incoming["budget"] = ents.get("money") if ents.get("money") is not None else ents.get("budget")
            except Exception:  # noqa: BLE001
                pass
        after = self._absorb_domain_slots(domain_id, text, dict(slots), None)
        before = self._user_slots(slots)
        for k, v in self._user_slots(after).items():
            if k not in before or str(before.get(k)) != str(v):
                incoming.setdefault(k, v)
        return incoming

    def _pause_for_slot_conflict(self, root_id, root, slots, conflicts, raw_text) -> dict:
        lines = [f"{k} 已确认={self._fmt_val(c['current'])}，新输入={self._fmt_val(c['incoming'])}"
                 for c in conflicts for k in [c["name"]]]
        opts = [
            confirm_mod.ConfirmationOption(
                "use_new", "采用我这次的新值",
                impact="覆盖先前已确认的参数并据此继续，旧值作废", risk="medium"),
            confirm_mod.ConfirmationOption(
                "keep_old", "保持原来的值",
                impact="忽略本次新输入，沿用先前已确认参数继续", risk="low"),
            confirm_mod.ConfirmationOption(
                "cancel", "先暂停，我再说明", impact="不修改参数、不继续执行", risk="low"),
        ]
        payload = self._pause_confirmation(
            root_id, confirm_mod.PAUSE_PREFERENCE,
            problem="您这次的输入与已确认信息冲突：" + "；".join(lines),
            decision_needed="应以哪个值为准？",
            question="检测到信息冲突，请确认采用哪个值：",
            options=opts,
            confirmed_info=[f"{k} = {self._fmt_val(v)}" for k, v in self._user_slots(slots).items()],
            context={"conflicts": conflicts}, wait_status=WAITING_USER,
            extra_result={"slot_conflict_pending": {"conflicts": conflicts, "raw_text": raw_text}})
        return {"status": WAITING_USER, "slot_conflict": conflicts,
                "confirmation": payload.get("confirmation")}

    @staticmethod
    def _fmt_val(v) -> str:
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v)

    def _absorb_domain_slots(self, domain_id: str, text: str, slots: dict,
                             slot_updates: dict | None) -> dict:
        """从澄清回答中抽取领域槽位（如能源类型），识别"全部比较"。"""
        out = dict(slots)
        if slot_updates:
            out.update(slot_updates)
            return out
        pack = dom_mod.load_default_registry().get(domain_id)
        dom = (pack.scenario_cfg.get("domain", {}) if pack else {}) or {}
        branch = dom.get("branch_on")
        if branch:
            name = branch.get("slot")
            opts = branch.get("options", [])
            low = (text or "").strip().lower()
            if coord_mod.is_all_branches(text):
                out[name] = "all"
                return out
            for o in opts:
                lbl = str(o.get("label", ""))
                val = str(o.get("value", ""))
                aliases = [a for a in o.get("aliases", []) or []]
                cands = [lbl, val, *aliases]
                hit = val == low or any(
                    (c and (c in (text or "") or (text or "").strip() in c))
                    for c in cands)
                if hit:
                    out[name] = o["value"]
                    break
        # 金额/数字通用抽取（预算等）
        try:
            ents = RouterAgent_extract(text)
            if ents.get("money") is not None and "budget" not in out:
                out["budget"] = ents["money"]
        except Exception:  # noqa: BLE001
            pass
        return out

    def cancel(self, root_id: str, reason: str = "用户取消") -> dict:
        root = db.get_task(self.conn, root_id)
        if root is None or root.status in TERMINAL_STATUSES:
            return {"status": root.status if root else "not_found"}
        for c in db.list_children(self.conn, root_id):
            if c.status not in TERMINAL_STATUSES:
                db.update_task(self.conn, c.id, status=CANCELLED,
                               result={"cancelled_reason": reason})
        done = self.conn.execute(
            """SELECT detail_json FROM events WHERE kind='tool_result'
               AND json_extract(detail_json,'$.idempotency_key') IS NOT NULL
               AND json_extract(detail_json,'$.ok')=1
               AND task_id IN (SELECT id FROM tasks WHERE root_id=?)""",
            (root_id,),
        ).fetchall()
        irreversible = [json.loads(r["detail_json"]).get("result") for r in done]
        db.update_task(self.conn, root_id, status=CANCELLED,
                       result={"cancelled_reason": reason, "irreversible_side_effects": irreversible})
        events.emit(self.conn, events.TASK_STATUS, task_id=root_id, actor="user",
                    detail={"status": CANCELLED, "reason": reason,
                            "irreversible_count": len(irreversible)})
        return {"status": CANCELLED, "irreversible_side_effects": irreversible}

    def decide_approval(self, approval_id: str, decision: str, by: str = "user") -> dict:
        row = self.conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            raise FileNotFoundError(approval_id)
        if row["status"] != "pending":
            return {"status": row["status"]}
        if decision not in ("approved", "rejected"):
            raise ValueError("decision 必须是 approved 或 rejected")
        self.conn.execute(
            "UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=?",
            (decision, by, db.now(), approval_id),
        )
        self.conn.commit()
        events.emit(self.conn, events.APPROVAL_DECIDED, task_id=row["task_id"], actor=by,
                    detail={"approval_id": approval_id, "decision": decision,
                            "tool": row["tool_name"]})
        root = db.get_task(self.conn, row["task_id"])
        self.tick(root.root_id)
        return {"status": decision}


# ---------------------------------------------------------------------------
# 模块级函数式入口（供评测/外部调用；内部委托 Engine）
# ---------------------------------------------------------------------------

def _registry_for_conn(conn, cfg):
    """每个连接缓存一个工具注册表（核心+已启用插件），避免多次构建与双注册表不一致。"""
    cache = getattr(conn, "_lf_registry", None)
    if cache is not None:
        return cache
    from .tools import ToolRegistry, register_core_tools
    registry = ToolRegistry()
    register_core_tools(registry)
    try:
        from .plugins import loader as _ploader
        plugins = _ploader.load_plugins(cfg)
        for tspec, lp in _ploader.collect_tools(plugins):
            registry.register(tspec, data_dir=lp.data_dir,
                              config=(cfg.get("plugins", {}).get(lp.name, {}) or {}).get("config", {}))
    except Exception:  # noqa: BLE001
        pass
    try:
        conn._lf_registry = registry  # sqlite Connection 允许挂属性
    except Exception:  # noqa: BLE001
        pass
    return registry


def _engine(conn, cfg=None, registry=None):
    from .tools import ToolRuntime, ToolResultInDoubt
    cfg = cfg or cfg_mod.load_config()
    registry = registry or _registry_for_conn(conn, cfg)
    runtime = ToolRuntime(conn, registry,
                          timeout_seconds=cfg.get("limits", {}).get("tool_timeout_seconds", 30))
    return Engine(conn, cfg, runtime)


def create_goal(conn, goal_or_payload, scenario=None, slots=None, cfg=None):
    """模块级建目标。支持 (conn, goal, scenario, slots) 与 (conn, {goal, scenario, slots})。"""
    if isinstance(goal_or_payload, dict):
        goal = goal_or_payload.get("goal")
        scenario = goal_or_payload.get("scenario")
        slots = goal_or_payload.get("slots")
    else:
        goal = goal_or_payload
    eng = _engine(conn, cfg)
    return eng.create_goal(goal, scenario, slots)


def create_root_task(conn, goal, scenario, slots=None, cfg=None):
    return create_goal(conn, goal, scenario, slots, cfg)


def run_root(conn, root_id, cfg=None):
    """可重入的推进入口（重启后以新连接调用同一 DB 即可恢复）。"""
    eng = _engine(conn, cfg)
    return eng.tick(root_id)


def decide_approval(conn, approval_id, decision, args=None, cfg=None):
    eng = _engine(conn, cfg)
    # args 仅用于绑定校验；批准绑定在请求时已落库，这里忽略额外参数
    return eng.decide_approval(approval_id, decision if isinstance(decision, str)
                               else decision.get("decision", "approved"))
