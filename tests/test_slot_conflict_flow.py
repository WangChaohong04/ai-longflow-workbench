"""澄清中给出与已确认槽位矛盾的值 -> 必须请求确认，不静默覆盖。"""
from __future__ import annotations

from longflow import domains as dom_mod
from longflow.models import WAITING_USER
from tests.conftest import tmp_db


def _drive(eng, rid, n=20):
    last = None
    for _ in range(n):
        last = eng.tick(rid)["status"]
        if last in ("completed", "partially_completed", "failed",
                    "waiting_user", "waiting_external", "cancelled"):
            break
    return last


def test_budget_conflict_triggers_confirmation(workdir):
    s = tmp_db(workdir)
    eng = s.engine()
    reg = dom_mod.load_default_registry()
    # 明确预算 8 万 + 汽车 -> 缺能源类型，等待澄清（已确认 budget=80000）
    r = eng.create_domain_goal("预算8万以内家用车", registry=reg)
    rid = r["task_id"]
    root = s.task(rid)
    assert root["status"] == WAITING_USER
    # 澄清回答里同时给了能源类型和一个矛盾的新预算（5万）
    out = eng.user_message(rid, "纯电，预算改成5万")
    # 必须停在冲突确认，且不静默覆盖
    assert "slot_conflict" in out
    root = s.task(rid)
    assert root["status"] == WAITING_USER
    conf = root["result"]["confirmation"]
    assert conf["reason_code"] in ("preference_unclear", "missing_info")
    # 旧值仍保留（未被覆盖）
    assert float(root["slots"].get("budget", 0)) == 80000.0
    assert any("80000" in line or "8" in line for line in conf["confirmed_info"])


def test_conflict_choose_keep_old_then_continues(workdir):
    s = tmp_db(workdir)
    eng = s.engine()
    reg = dom_mod.load_default_registry()
    r = eng.create_domain_goal("预算8万以内家用车", registry=reg)
    rid = r["task_id"]
    eng.user_message(rid, "纯电，预算改成5万")  # 触发冲突
    # 选择保持原值
    out = eng.user_message(rid, "保持原来的值")
    root = s.task(rid)
    assert "slot_conflict_pending" not in (root["result"] or {})
    assert float(root["slots"]["budget"]) == 80000.0
    # 能源类型已在上一轮抽取；继续推进（不再卡在冲突）
    assert root["status"] in ("in_progress", "partially_completed", "completed",
                              "waiting_user", "waiting_external")


def test_conflict_choose_new_overwrites(workdir):
    s = tmp_db(workdir)
    eng = s.engine()
    reg = dom_mod.load_default_registry()
    r = eng.create_domain_goal("预算8万以内家用车", registry=reg)
    rid = r["task_id"]
    eng.user_message(rid, "纯电，预算改成5万")
    out = eng.user_message(rid, "采用我这次的新值")
    root = s.task(rid)
    assert "slot_conflict_pending" not in (root["result"] or {})
    assert float(root["slots"]["budget"]) == 50000.0
