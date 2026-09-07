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
import sqlite3

from . import config as cfg_mod
from . import db, events, llm, permissions, rag
from .models import (
    CANCELLED,
    COMPLETED,
    FAILED,
    IN_PROGRESS,
    PENDING,
    READY,
    TERMINAL_STATUSES,
    WAITING_APPROVAL,
    WAITING_EVENT,
    ROLE_CONTROLLER,
    ROLE_EXECUTOR,
    ROLE_RESEARCHER,
    ROLE_VERIFIER,
)
from .tools import ToolRuntime

EMOTION_HINTS = ["非常生气", "愤怒", "投诉", "差评"]


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

    # ---------- 创建任务与入口闸门 ----------

    def create_goal(self, goal_text: str, scenario: str, slots: dict | None = None) -> dict:
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
        )
        self.conn.execute("UPDATE tasks SET root_id=? WHERE id=?", (root_id, root_id))
        self.conn.commit()
        events.emit(self.conn, events.TASK_CREATED, task_id=root_id, actor="user",
                    detail={"goal": goal_text, "scenario": scenario, "slots": merged_slots})

        permissions.preauth_from_policy(self.conn, scenario_cfg.get("policies", {}), task_id=root_id)

        gate = self._gate_entry(root_id, goal_text, scenario_cfg, merged_slots)
        if gate["verdict"] == "clarify":
            db.update_task(self.conn, root_id, status=WAITING_EVENT,
                           result={"clarify": gate["missing_slots"], "gate": "entry",
                                   "message": "需要补充信息才能继续"})
            events.emit(self.conn, events.CLARIFY, task_id=root_id, actor="controller",
                        detail={"missing_slots": gate["missing_slots"]})
        elif gate["verdict"] == "handoff":
            db.update_task(self.conn, root_id, status=WAITING_EVENT,
                           result={"handoff": True, "message": gate["message"], "gate": "entry"})
        else:
            self._materialize_plan(root_id, gate["plan"])
        return {"task_id": root_id, **gate}

    def _gate_entry(self, root_id: str, goal: str, scenario_cfg: dict, slots: dict) -> dict:
        emotion = any(h in goal for h in EMOTION_HINTS)
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

        steps = 0
        max_steps = self.limits.get("max_steps", 30)
        advanced_any = False

        self._resume_approved(root_id)

        while steps < max_steps:
            children = db.list_children(self.conn, root_id)
            self._cascade_failures(children)
            children = db.list_children(self.conn, root_id)

            actionable = self._next_actionable(children)
            if actionable is None:
                break
            advanced = self._run_subtask(actionable, root)
            advanced_any = advanced_any or advanced
            steps += 1
            self._resume_approved(root_id)

        children = db.list_children(self.conn, root_id)
        if children and all(c.status in TERMINAL_STATUSES for c in children):
            self._gate_exit(db.get_task(self.conn, root_id))
        elif not children and root.status not in (WAITING_EVENT, WAITING_APPROVAL):
            self._gate_exit(db.get_task(self.conn, root_id))

        return {"root_id": root_id, "status": db.get_task(self.conn, root_id).status,
                "advanced": advanced_any}

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
            if c.status in (PENDING, READY, IN_PROGRESS, WAITING_APPROVAL, WAITING_EVENT):
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
        for c in children:
            if c.status in (PENDING, READY, IN_PROGRESS):
                if c.status == PENDING:
                    deps_done = all(
                        any(d.id == dep and d.status == COMPLETED for d in children)
                        for dep in c.depends_on
                    )
                    if not deps_done:
                        continue
                    db.update_task(self.conn, c.id, status=READY)
                    c = db.get_task(self.conn, c.id)
                return c
        return None

    # ---------- 子任务执行 ----------

    def _build_action(self, task, root) -> dict:
        """决定子任务下一步动作。模板 tool_args 优先（场景配置），否则交给 LLM 驱动。"""
        tool = task.plan.get("tool_hint")
        tool_args = task.plan.get("tool_args")
        if tool and tool_args:
            return {"type": llm.ACTION_TOOL_CALL, "tool": tool,
                    "args": _coerce_numbers(_interp(tool_args, task.slots, root.objective)), "reason": task.objective}
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
        if task.status != IN_PROGRESS:
            db.update_task(self.conn, task.id, status=IN_PROGRESS)
            events.emit(self.conn, events.TASK_STATUS, task_id=task.id,
                        actor=f"agent:{task.agent_role}", detail={"status": IN_PROGRESS})

        action = self._build_action(task, root)
        events.emit(self.conn, events.LLM_CALL, task_id=task.id,
                    actor=f"agent:{task.agent_role}",
                    detail={"action": action.get("type"), "tool": action.get("tool")})
        try:
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
            db.update_task(self.conn, task.id, status=WAITING_APPROVAL,
                           result={"pending_approval": exc.approval_id, "tool": exc.tool_name})
            return False  # 不阻塞其他分支
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
        chunks, geo = self._gather_evidence(task)

        if geo is not None and task.agent_role == ROLE_VERIFIER:
            result = self._verify_geo(geo)
        elif geo is not None and task.agent_role == ROLE_RESEARCHER:
            result = {"geo": geo, "tool_result": geo}
        else:
            draft = self.driver.draft_answer(root_task.objective, chunks, task.slots)
            conflicts = self._detect_conflicts(chunks)
            citation_check = self._verify_citations(draft, chunks)
            result = {
                "answer": draft["text"],
                "citations": [
                    {"chunk_id": c["chunk_id"], "doc_name": c["doc_name"], "section": c["section"],
                     "citations": c.get("citations", []), "snippet": c["text"][:200]}
                    for c in chunks if c["chunk_id"] in draft.get("citations", [])
                ],
                "citation_check": citation_check,
                "conflicts": conflicts,
                "no_evidence": draft.get("no_evidence", not chunks),
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
            footer = "注：直线距离为 haversine 球面距离；未配置路线服务，不提供通勤时间；标注 source:null 的属性未核验。"
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
    def _verify_citations(draft: dict, chunks: list[dict]) -> dict:
        """出口闸门引用核验：引用必须存在，且其文本与答案有实质词重叠（支撑关系）。"""
        by_id = {c["chunk_id"]: c for c in chunks}
        problems = []
        for cid in draft.get("citations", []):
            ch = by_id.get(cid)
            if ch is None:
                problems.append({"chunk_id": cid, "problem": "引用不存在"})
                continue
            ans_terms = {t for t in rag.tokenize(draft["text"]) if len(t) > 1}
            ch_terms = set(rag.tokenize(ch["text"]))
            overlap = len(ans_terms & ch_terms)
            if overlap < 2:
                problems.append({"chunk_id": cid, "problem": "引用内容与答案缺乏支撑关系",
                                 "overlap": overlap})
        return {"passed": not problems, "problems": problems}

    @staticmethod
    def _detect_conflicts(chunks: list[dict]) -> list[dict]:
        """检测不同版本资料对同一指标给出冲突数值（如 2023/2024 住宿标准）。"""
        num_re = re.compile(r"(\d+(?:\.\d+)?)\s*(元|块|k|km|公里|%)?")
        by_doc: dict[str, list] = {}
        for ch in chunks:
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
            if r.get("citation_check") and not r["citation_check"]["passed"]:
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

        # 规则 1：高风险动作必须有审批记录
        high_risk_done = self.conn.execute(
            """SELECT COUNT(*) AS n FROM events WHERE kind='tool_result'
               AND json_extract(detail_json,'$.tool')='make_purchase'
               AND json_extract(detail_json,'$.ok')=1
               AND task_id IN (SELECT id FROM tasks WHERE root_id=?)""",
            (root.id,),
        ).fetchone()["n"]
        approvals_ok = self.conn.execute(
            "SELECT COUNT(*) AS n FROM approvals WHERE status='approved' AND task_id IN (SELECT id FROM tasks WHERE root_id=?)",
            (root.id,),
        ).fetchone()["n"]
        if high_risk_done and not approvals_ok:
            findings["passed"] = False
            findings["problems"].append({"kind": "governance", "detail": "高风险动作缺少审批记录"})

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

        result = {
            "answer": answer_text,
            "verified": findings["passed"],
            "verified_partial": findings["verified_partial"],
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
        events.emit(self.conn, events.GATE_EXIT, task_id=root.id, actor="verifier",
                    detail={"passed": findings["passed"], "problems": findings["problems"],
                            "verified_partial": findings["verified_partial"],
                            "pending_approvals": len(pending_approvals)})

    # ---------- 用户消息/澄清/取消/审批 ----------

    def user_message(self, root_id: str, text: str, slot_updates: dict | None = None) -> dict:
        root = db.get_task(self.conn, root_id)
        if root is None:
            raise FileNotFoundError(root_id)
        events.emit(self.conn, events.MESSAGE, task_id=root_id, actor="user",
                    detail={"text": text, "slots": slot_updates or {}})
        slots = dict(root.slots)
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

        if root.status == WAITING_EVENT and root.result.get("clarify"):
            still_missing = [m for m in root.result["clarify"] if not slots.get(m["name"])]
            if still_missing:
                return {"status": WAITING_EVENT,
                        "still_missing": [m["name"] for m in still_missing]}
            scenario_cfg = cfg_mod.load_scenario(slots.get("__scenario__", ""))
            gate = self._gate_entry(root_id, root.objective, scenario_cfg, slots)
            db.set_task_result_full(self.conn, root_id, {})
            if gate["verdict"] == "pass":
                self._materialize_plan(root_id, gate["plan"])
        return self.tick(root_id)

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
    from .tools import ToolRuntime
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