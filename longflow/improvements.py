"""受控自优化与持续演进（评测驱动；禁止系统自动改生产）。

职责边界（硬性）：
- 只**记录**任务结束轨迹（目标/路由/领域/subagent/来源/工具/反馈/成本时延/是否决策就绪）；
- 对失败做**归因**（10 类，见 ATTR_*）；
- 据归因生成**候选改进**（prompt 候选版、槽位/澄清规则、检索关键词/过滤/重排、
  知识版本、工具参数/错误处理、subagent 调度），候选只落库为 `proposed`。

系统**绝不**自动：
- 修改自身代码、权限边界、审批规则、数据访问范围；
- 直接发布新 Prompt 到生产；
- 把未经审核的用户反馈写入正式知识库。
候选改进必须先过独立评测集回归、与生产版本对比，再经人工审核 + 灰度才能 `rolled_out`；
每次变更保存版本、原因、评测结果、适用范围与回滚方式。
"""
from __future__ import annotations

import json
import time
from typing import Any

from . import db

# 10 类失败归因
ATTR_ROUTING = "routing_error"              # 路由错误
ATTR_CLARIFY = "insufficient_clarification"  # 澄清不足
ATTR_SLOT = "slot_extraction_error"         # 槽位提取错误
ATTR_RAG_MISS = "rag_no_recall"             # RAG 未召回
ATTR_RAG_RANK = "rag_ranking_error"         # RAG 召回但排序错误
ATTR_GENERATION = "unsupported_generation"  # 生成内容无证据
ATTR_TOOL = "tool_failure"                  # 工具调用失败
ATTR_PERMISSION = "permission_issue"        # 权限/审批问题
ATTR_SOURCE = "external_source_unavailable"  # 外部数据源不可用
ATTR_UI = "ui_interaction"                  # 前端状态/交互
ATTRIBUTIONS = {ATTR_ROUTING, ATTR_CLARIFY, ATTR_SLOT, ATTR_RAG_MISS, ATTR_RAG_RANK,
                ATTR_GENERATION, ATTR_TOOL, ATTR_PERMISSION, ATTR_SOURCE, ATTR_UI}

# 候选改进类型
IMPROVEMENT_KINDS = {"prompt", "slot_rule", "retrieval", "knowledge", "tool", "subagent"}

# 候选生命周期
ST_PROPOSED = "proposed"      # 已登记（默认，不生效）
ST_EVALUATED = "evaluated"    # 已过独立评测
ST_APPROVED = "approved"      # 人工审核通过
ST_ROLLED_OUT = "rolled_out"  # 灰度启用
ST_REJECTED = "rejected"
ST_ROLLED_BACK = "rolled_back"


def record_run(conn, *, goal, final_status, route=None, domains=None, subagents=None,
               sources=None, evidence_count=0, tool_calls=None, tool_failures=None,
               user_outcome=None, feedback=None, latency_ms=None, llm_calls=0,
               decision_ready=False, attributions=None) -> str:
    """记录一次任务结束的完整轨迹（供归因与评测，不影响生产行为）。"""
    rid = db.new_id("run")
    conn.execute(
        """INSERT INTO run_telemetry
           (id, root_id, goal, final_status, route_json, domains_json, subagents_json,
            sources_json, evidence_count, tool_calls_json, tool_failures_json, user_outcome,
            feedback, latency_ms, llm_calls, decision_ready, attributions_json, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rid, (route or {}).get("task_id") or None, goal, final_status,
         json.dumps(route, ensure_ascii=False), json.dumps(domains or [], ensure_ascii=False),
         json.dumps(subagents or [], ensure_ascii=False), json.dumps(sources or [], ensure_ascii=False),
         int(evidence_count or 0), json.dumps(tool_calls or [], ensure_ascii=False),
         json.dumps(tool_failures or [], ensure_ascii=False), user_outcome, feedback,
         int(latency_ms) if latency_ms else None, int(llm_calls or 0),
         1 if decision_ready else 0,
         json.dumps(attributions or [], ensure_ascii=False), db.now()),
    )
    conn.commit()
    return rid


def attribute_failure(*, route=None, missing_slots=None, slot_errors=None,
                      chunks=None, evidence=None, tool_failures=None,
                      permission_issues=None, source_errors=None, verdict=None,
                      answer_has_unsupported=None, ui_issue=False) -> list[str]:
    """确定性归因（可多因）。只依据可观测信号，不臆测。"""
    out = []
    if route and route.get("low_confidence") and not route.get("domains"):
        out.append(ATTR_ROUTING)
    if missing_slots and route and route.get("needed_clarify") and not route.get("clarified"):
        out.append(ATTR_CLARIFY)
    if slot_errors:
        out.append(ATTR_SLOT)
    if chunks is not None and len(chunks) == 0:
        out.append(ATTR_RAG_MISS)
    if evidence and evidence.get("counts", {}).get("unconfirmed") and evidence.get("verdict") == "failed":
        # 有召回但答案论断无据 -> 生成问题
        if answer_has_unsupported:
            out.append(ATTR_GENERATION)
    if verdict == "partial" and chunks:
        out.append(ATTR_RAG_RANK)
    if tool_failures:
        out.append(ATTR_TOOL)
    if permission_issues:
        out.append(ATTR_PERMISSION)
    if source_errors:
        out.append(ATTR_SOURCE)
    if ui_issue:
        out.append(ATTR_UI)
    return sorted(set(out))


def propose_improvement(conn, *, kind: str, target: str, rationale: str,
                        attribution: str, payload: dict, scope: str = "") -> str:
    """登记一条候选改进。强制 status=proposed；系统无权自行 rollout。"""
    if kind not in IMPROVEMENT_KINDS:
        raise ValueError(f"非法改进类型: {kind}")
    if attribution and attribution not in ATTRIBUTIONS:
        raise ValueError(f"非法归因码: {attribution}")
    iid = db.new_id("imp")
    conn.execute(
        """INSERT INTO improvements
           (id, kind, target, rationale, attribution, payload_json, status, scope,
            rollback, version, created_at)
           VALUES (?,?,?,?,?,?, 'proposed', ?, 'revert to current production config/prompt', ?, ?)""",
        (iid, kind, target, rationale, attribution,
         json.dumps(payload, ensure_ascii=False), scope,
         f"cand-{db.now()}", db.now()),
    )
    conn.commit()
    return iid


def suggest_improvements(conn, attributions: list[str], *, context: dict | None = None) -> list[str]:
    """据归因**建议**候选改进（只登记，不应用）。返回改进 id。"""
    context = context or {}
    ids = []
    mapping = {
        ATTR_RAG_MISS: ("retrieval", "kb_search", "RAG 零召回：扩充查询改写/同义词/字段过滤"),
        ATTR_RAG_RANK: ("retrieval", "kb_search", "召回但排序不佳：调整重排权重/引用字段加分"),
        ATTR_SLOT: ("slot_rule", "slots", "槽位提取错误：补充类型/正则/澄清问题"),
        ATTR_CLARIFY: ("slot_rule", "clarify", "澄清不足：拆分关键槽位、每轮 1-2 问"),
        ATTR_GENERATION: ("prompt", "draft_answer", "无据生成：强化'无证据即明说'约束"),
        ATTR_TOOL: ("tool", "tool_runtime", "工具失败：补超时/重试/错误处理策略"),
        ATTR_PERMISSION: ("subagent", "permissions", "权限问题：复核工具风险分级与授权范围"),
        ATTR_SOURCE: ("knowledge", "external_sources", "外部源不可用：增加备选源/缓存/降级标注"),
        ATTR_ROUTING: ("prompt", "router", "路由误判：补充领域语义提示/触发词/置信阈值"),
        ATTR_UI: ("subagent", "frontend", "前端交互：状态/草稿/待确认展示修复"),
    }
    for attr in attributions:
        if attr in mapping:
            kind, target, rationale = mapping[attr]
            ids.append(propose_improvement(
                conn, kind=kind, target=target, rationale=rationale,
                attribution=attr, payload={"context": context}, scope="需独立评测+人工审核+灰度"))
    return ids


# ---- 评测/审核/灰度：仅人工显式推进；系统不自动调用 ----

def set_evaluation(conn, improvement_id: str, eval_result: dict, *, by: str) -> None:
    _update(conn, improvement_id, status=ST_EVALUATED, eval_result=eval_result, by=by)


def approve(conn, improvement_id: str, *, by: str) -> None:
    _update(conn, improvement_id, status=ST_APPROVED, by=by)


def roll_out(conn, improvement_id: str, *, by: str) -> None:
    """灰度启用：必须先 approved。生产是否真正应用由人工/部署决定，本系统不自动改代码。"""
    row = conn.execute("SELECT status FROM improvements WHERE id=?", (improvement_id,)).fetchone()
    if row is None:
        raise FileNotFoundError(improvement_id)
    if row["status"] != ST_APPROVED:
        raise PermissionError(f"改进 {improvement_id} 状态 {row['status']}，未审核通过不得灰度")
    _update(conn, improvement_id, status=ST_ROLLED_OUT, by=by)


def roll_back(conn, improvement_id: str, *, by: str) -> None:
    _update(conn, improvement_id, status=ST_ROLLED_BACK, by=by)


def _update(conn, improvement_id: str, *, status: str, by: str, eval_result: dict | None = None) -> None:
    conn.execute(
        """UPDATE improvements SET status=?, decided_at=?, decided_by=?,
           eval_result_json=COALESCE(?, eval_result_json) WHERE id=?""",
        (status, db.now(), by,
         json.dumps(eval_result, ensure_ascii=False) if eval_result is not None else None,
         improvement_id),
    )
    conn.commit()


def list_improvements(conn, status: str | None = None) -> list[dict]:
    if status:
        rows = conn.execute("SELECT * FROM improvements WHERE status=? ORDER BY id", (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM improvements ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("payload_json", "eval_result_json"):
            d[k.replace("_json", "")] = json.loads(d.pop(k) or "null")
        out.append(d)
    return out
