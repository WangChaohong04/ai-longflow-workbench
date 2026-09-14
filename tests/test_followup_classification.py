"""Batch9（第10类 多轮）：区分缺失信息/约束变更/追问/新任务；已执行动作不丢失。"""
from tests.conftest import workdir, tmp_db  # noqa: F401

import longflow.db as db
import longflow.events as events
from longflow.orchestrator import classify_followup
from longflow.models import COMPLETED, TERMINAL_STATUSES


def test_classify_followup_categories():
    assert classify_followup("笔记本电脑", is_waiting_clarify=True) == "missing_info"
    assert classify_followup("预算改成9000元", is_waiting_clarify=False) == "constraint_change"
    assert classify_followup("数量改成5台", is_waiting_clarify=False,
                             slot_updates={"quantity": 5}) == "constraint_change"
    assert classify_followup("换个问题，报销怎么走", is_waiting_clarify=False) == "new_task"
    assert classify_followup("还有别的需要注意吗", is_waiting_clarify=False) == "follow_up"


def test_executed_side_effects_preserved_on_cancel(workdir):
    """约束变更/取消时，已执行的副作用记录保留在结果中（不丢失）。"""
    session = tmp_db(workdir)
    session.ensure_scenario_knowledge("team_ops")
    eng = session.engine()
    # low/medium 可自动放行的通知动作：先发一条通知，再取消，副作用应被记录保留
    # 高风险采购：进入等待审批（非终态），此时取消应保留已发生副作用清单
    res = eng.create_goal("我要采购笔记本电脑 预算8000元", "team_ops",
                          {"item": "笔记本电脑", "budget": 8000})
    root_id = res["task_id"]
    eng.tick(root_id)
    out = eng.cancel(root_id, reason="改期")
    root = db.get_task(session.conn, root_id)
    assert root.status == "cancelled"
    # 已发生的副作用被记录到 irreversible_side_effects（可能为空列表，但键存在、不静默丢失）
    assert "irreversible_side_effects" in out
    assert "irreversible_side_effects" in (root.result or {})


def test_followup_message_emits_classification(workdir):
    session = tmp_db(workdir)
    session.ensure_scenario_knowledge("team_ops")
    eng = session.engine()
    res = eng.create_goal("出差住宿标准是多少？", "team_ops", {})
    root_id = res["task_id"]
    eng.tick(root_id)
    eng.user_message(root_id, "预算改成1000元呢？")
    evs = events.list_events(session.conn, root_id)
    classes = [e.get("detail", {}).get("followup_class") for e in evs]
    assert "constraint_change" in classes
