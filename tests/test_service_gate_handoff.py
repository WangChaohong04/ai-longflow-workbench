"""Batch9（第8类 服务闭环闸门）：
- 强情绪单独不触发人工转接（情绪只是观测信号，转接须用户明确要求）；
- 明确要求转人工：配置了渠道才报"已转接"，未配置如实说明不可用（不谎称已转）；
- 高风险动作在出口闸门要求审批；信息不足时只问一条具体澄清。
"""
from tests.conftest import workdir, tmp_db  # noqa: F401

import longflow.db as db
from longflow.models import WAITING_EVENT, WAITING_APPROVAL


def test_emotion_alone_does_not_handoff(workdir):
    session = tmp_db(workdir)
    session.ensure_scenario_knowledge("team_ops")
    eng = session.engine()
    # 强情绪 + 明确采购诉求：应正常进入流程（pass），而非转人工
    res = eng.create_goal("我非常生气，你们太差劲了！我要采购笔记本电脑 预算8000元",
                          "team_ops", {"item": "笔记本电脑", "budget": 8000})
    assert res["verdict"] != "handoff", "强情绪+明确诉求不应转人工"
    assert res["verdict"] in ("pass", "clarify")

    # 只有情绪、无人工字样、无具体可办诉求：也不得谎称已转人工
    res2 = eng.create_goal("我要投诉你们！差评！", "team_ops", {})
    if res2["verdict"] == "handoff":
        # 即便判定 handoff，也必须如实说明渠道状态，不得谎称"已转接"
        assert "未配置" in res2.get("message", "") or "无法" in res2.get("message", "")


def test_explicit_handoff_channel_states(workdir):
    session = tmp_db(workdir)
    session.ensure_scenario_knowledge("team_ops")
    # 未配置人工渠道
    eng = session.engine()
    eng.cfg["human_channel"] = None
    res = eng.create_goal("给我转人工客服", "team_ops", {})
    assert res["verdict"] == "handoff"
    assert "未配置" in res["message"] or "无法" in res["message"], "渠道不可用须如实说明"

    # 配置了渠道
    eng.cfg["human_channel"] = "ops-im"
    res2 = eng.create_goal("我要找真人客服", "team_ops", {})
    assert res2["verdict"] == "handoff"
    assert "ops-im" in res2["message"]


def test_missing_info_single_clarification(workdir):
    session = tmp_db(workdir)
    session.ensure_scenario_knowledge("team_ops")
    eng = session.engine()
    # 采购但缺 item/budget：入口闸门应要求澄清
    res = eng.create_goal("我要采购", "team_ops", {})
    assert res["verdict"] == "clarify"
    missing = res.get("missing_slots", [])
    assert missing, "应指出缺失的槽位"


def test_high_risk_requires_approval_before_execution(workdir):
    session = tmp_db(workdir)
    session.ensure_scenario_knowledge("team_ops")
    eng = session.engine()
    res = eng.create_goal("我要采购笔记本电脑 预算8000元", "team_ops",
                          {"item": "笔记本电脑", "budget": 8000})
    root_id = res["task_id"]
    eng.tick(root_id)
    children = db.list_children(session.conn, root_id)
    # 高风险下单在执行前进入等待审批（而不是直接完成下单）
    assert any(c.status == WAITING_APPROVAL for c in children), \
        "高风险动作必须在执行前等待人工审批"
