"""批次5：受控自优化——轨迹记录、10 类归因、候选改进只登记不自动生效。"""
from __future__ import annotations

import pytest

from longflow import improvements as IMP
from longflow import db


def _conn(workdir):
    conn = db.connect(str(workdir / "imp.db"))
    db.init_db(conn)
    return conn


def test_record_run_persists_telemetry(workdir):
    conn = _conn(workdir)
    rid = IMP.record_run(
        conn, goal="采购笔记本", final_status="waiting_approval",
        route={"task_id": "r1", "mode": "await_confirmation"},
        domains=["team_ops"], subagents=["rag_researcher"], evidence_count=3,
        tool_calls=[{"tool": "kb_search"}], tool_failures=[],
        user_outcome="pending", latency_ms=120, llm_calls=0, decision_ready=False)
    row = conn.execute("SELECT * FROM run_telemetry WHERE id=?", (rid,)).fetchone()
    assert row["final_status"] == "waiting_approval"
    assert row["evidence_count"] == 3 and row["decision_ready"] == 0


def test_attribution_covers_ten_categories():
    assert len(IMP.ATTRIBUTIONS) == 10
    # RAG 零召回
    assert IMP.ATTR_RAG_MISS in IMP.attribute_failure(chunks=[])
    # 工具失败
    assert IMP.ATTR_TOOL in IMP.attribute_failure(tool_failures=[{"tool": "http_get"}])
    # 权限
    assert IMP.ATTR_PERMISSION in IMP.attribute_failure(permission_issues=["high risk"])
    # 路由低置信
    assert IMP.ATTR_ROUTING in IMP.attribute_failure(
        route={"low_confidence": True, "domains": []})


def test_proposed_improvement_does_not_affect_production(workdir):
    conn = _conn(workdir)
    iid = IMP.propose_improvement(
        conn, kind="prompt", target="router", rationale="路由误判",
        attribution=IMP.ATTR_ROUTING, payload={"new_prompt": "..."})
    rows = IMP.list_improvements(conn)
    mine = [r for r in rows if r["id"] == iid][0]
    # 默认 proposed，绝不自动生效
    assert mine["status"] == IMP.ST_PROPOSED
    assert mine["rollback"]  # 每个候选必须带回滚方式
    assert mine["version"]


def test_invalid_kind_or_attribution_rejected(workdir):
    conn = _conn(workdir)
    with pytest.raises(ValueError):
        IMP.propose_improvement(conn, kind="code_change", target="x", rationale="r",
                                attribution=IMP.ATTR_TOOL, payload={})
    with pytest.raises(ValueError):
        IMP.propose_improvement(conn, kind="prompt", target="x", rationale="r",
                                attribution="made_up", payload={})


def test_rollout_requires_approval(workdir):
    conn = _conn(workdir)
    iid = IMP.propose_improvement(
        conn, kind="retrieval", target="kb_search", rationale="零召回",
        attribution=IMP.ATTR_RAG_MISS, payload={"synonyms": ["x"]})
    # 未评测/未审核 -> 不得灰度
    with pytest.raises(PermissionError):
        IMP.roll_out(conn, iid, by="system")
    # 人工流程：评测 -> 审核 -> 灰度
    IMP.set_evaluation(conn, iid, {"regression_pass": True, "delta": "+2 case"}, by="ci")
    IMP.approve(conn, iid, by="human-reviewer")
    IMP.roll_out(conn, iid, by="human-reviewer")
    assert IMP.list_improvements(conn, IMP.ST_ROLLED_OUT)
    # 可回滚
    IMP.roll_back(conn, iid, by="human-reviewer")
    assert [r for r in IMP.list_improvements(conn) if r["id"] == iid][0]["status"] == IMP.ST_ROLLED_BACK


def test_suggest_improvements_only_proposes(workdir):
    conn = _conn(workdir)
    ids = IMP.suggest_improvements(conn, [IMP.ATTR_RAG_MISS, IMP.ATTR_TOOL])
    assert len(ids) == 2
    assert all(r["status"] == IMP.ST_PROPOSED for r in IMP.list_improvements(conn))


def test_feedback_never_auto_written_to_knowledge(workdir):
    """受控自优化边界：用户反馈/候选内容不得自动进入知识库表。"""
    conn = _conn(workdir)
    secret = "用户反馈的临时内容-不应入库-9f3k2"
    IMP.propose_improvement(conn, kind="knowledge", target="kb", rationale=secret,
                            attribution=IMP.ATTR_SOURCE, payload={"note": secret})
    # knowledge_chunks 中不得出现该反馈内容
    rows = conn.execute("SELECT COUNT(*) AS n FROM knowledge_chunks WHERE text LIKE ?",
                        (f"%{secret}%",)).fetchone()
    assert rows["n"] == 0
