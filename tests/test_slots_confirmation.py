"""批次2：通用类型化槽位、澄清规则、5 段式用户确认、新任务状态。"""
from __future__ import annotations

import pytest

from longflow import slots as S
from longflow import confirmation as C
from longflow import models as M


# ---- 类型化槽位 ----

def test_coerce_money_and_number():
    assert S.coerce({"name": "budget", "type": "money"}, "8000元") == 8000.0
    assert S.coerce({"name": "n", "type": "number"}, "2万") == 20000.0
    assert S.coerce({"name": "n", "type": "number"}, 3.5) == 3.5


def test_coerce_number_range_and_invalid():
    assert S.coerce({"name": "r", "type": "number", "min": 1, "max": 50}, "10") == 10.0
    with pytest.raises(S.SlotError):
        S.coerce({"name": "r", "type": "number", "max": 50}, "100")
    with pytest.raises(S.SlotError):
        S.coerce({"name": "b", "type": "number"}, "abc")


def test_coerce_enum_and_bool():
    slot = {"name": "mode", "type": "enum", "enum": ["driving", "walking", "transit"]}
    assert S.coerce(slot, "walking") == "walking"
    with pytest.raises(S.SlotError):
        S.coerce(slot, "flying")
    assert S.coerce({"name": "ok", "type": "bool"}, "是") is True
    assert S.coerce({"name": "ok", "type": "bool"}, "不用") is False


def test_coerce_list_and_object():
    assert S.coerce({"name": "tags", "type": "list", "item_type": "text"}, "a, b、c") == ["a", "b", "c"]
    obj = S.coerce({"name": "addr", "type": "object",
                    "fields": [{"name": "city", "type": "text"}]}, {"city": "北京", "x": 1})
    assert obj["city"] == "北京"


def test_validate_all_collects_errors():
    defs = [{"name": "budget", "type": "money"}, {"name": "item", "type": "text"}]
    cleaned, errors = S.validate_all(defs, {"budget": "9000元", "item": "笔记本"})
    assert cleaned["budget"] == 9000.0 and cleaned["item"] == "笔记本"
    assert errors == []
    _, errors2 = S.validate_all(defs, {"budget": "很多钱"})
    assert any(e["name"] == "budget" for e in errors2)


def test_missing_slots_max_two_and_no_repeat():
    defs = [
        {"name": "a", "type": "text", "required_for": ["buy"], "prompt": "A?"},
        {"name": "b", "type": "text", "required_for": ["buy"], "prompt": "B?"},
        {"name": "c", "type": "text", "required_for": ["buy"], "prompt": "C?"},
    ]
    miss = S.missing_slots(defs, {"a": "x"}, "buy", max_ask=2)
    assert [m["name"] for m in miss] == ["b", "c"][:2] and len(miss) == 2
    # 已问过的不重复问
    miss2 = S.missing_slots(defs, {"a": "x"}, "buy", already_asked={"b"}, max_ask=2)
    assert [m["name"] for m in miss2] == ["c"]


def test_optional_slots_use_marked_defaults():
    defs = [{"name": "radius_km", "type": "number", "default": 2}]  # 无 required_for
    out = S.apply_defaults(defs, {})
    assert out["radius_km"] == 2
    # 缺失但可选 -> 不主动追问
    assert S.missing_slots(defs, {}, "buy") == []


def test_branch_all_detection():
    assert S.is_branch_all("都可以，你全部比较一下")
    assert S.is_branch_all("不确定")
    assert not S.is_branch_all("我要采购笔记本")


def test_slot_conflict_detected():
    conflicts = S.detect_slot_conflicts([], {"budget": 8000}, {"budget": 12000})
    assert conflicts and conflicts[0]["name"] == "budget"
    assert S.detect_slot_conflicts([], {"budget": 8000}, {"budget": 8000}) == []


# ---- 5 段式确认 ----

def test_irreversible_confirmation_has_five_parts():
    req = C.irreversible_action_confirmation("下单采购笔记本 8000 元",
                                             known=["物品=笔记本", "预算=8000 元"])
    d = req.to_dict()
    assert d["reason_code"] == C.PAUSE_IRREVERSIBLE
    assert d["confirmed_info"] and d["problem"] and d["decision_needed"]
    # 每个选项都必须带影响说明
    assert all(o["impact"] for o in d["options"])
    ids = {o["id"] for o in d["options"]}
    assert {"confirm", "modify", "cancel"} <= ids


def test_conflict_confirmation_options():
    req = C.evidence_conflict_confirmation(
        [{"entity": "住宿", "field": "限额", "values": [400, 500]}], known=["北京/上海出差"])
    assert req.reason_code == C.PAUSE_CONFLICT
    assert {o.id for o in req.options} >= {"use_latest", "show_both"}


def test_insufficient_evidence_confirmation():
    req = C.insufficient_evidence_confirmation("充电桩是否营业", known=["地点=望京"])
    assert req.reason_code == C.PAUSE_NO_EVIDENCE
    assert any(o.id == "state_gap" for o in req.options)


def test_preference_compare_all_option():
    req = C.preference_confirmation("优先便宜还是近？",
                                    [("cheap", "优先便宜", "可能更远"),
                                     ("near", "优先近", "可能更贵")], known=[])
    assert any(o.id == "compare_all" for o in req.options)


def test_unknown_pause_code_rejected():
    with pytest.raises(ValueError):
        C.build_confirmation("not_a_reason")


# ---- 新任务状态 ----

def test_new_waiting_statuses_recognized():
    for st in (M.WAITING_USER, M.WAITING_EXTERNAL, M.RETRYING, M.PARTIALLY_COMPLETED):
        assert st in M.RESUMABLE_STATUSES
    assert M.WAITING_USER in M.WAITING_STATUSES and M.WAITING_EXTERNAL in M.WAITING_STATUSES
    assert M.PARTIALLY_COMPLETED not in M.TERMINAL_STATUSES
    assert M.STATUS_LABELS[M.WAITING_USER] == "待用户处理"
