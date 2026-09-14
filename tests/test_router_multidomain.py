"""批次6补充：多领域路由、低置信澄清、上下文连续、确认优先（不越权）。"""
from __future__ import annotations

from longflow import domains as dom_mod
from longflow.router import RouterAgent, MODE_MULTI, MODE_CLARIFY, MODE_AWAIT, MODE_ACTIVATE


def _router_with_three():
    reg = dom_mod.DomainRegistry()
    reg.register(dom_mod.DomainPack(
        id="cars", name="汽车", description="选车购车",
        trigger_keywords=["车", "购车", "4S店", "续航"],
        semantic_hints=["汽车", "新能源", "充电"], slots=[], subagents=["rag_researcher"]))
    reg.register(dom_mod.DomainPack(
        id="charging", name="充电设施", description="充电桩充电站",
        trigger_keywords=["充电桩", "充电站", "充电"],
        semantic_hints=["充电", "快充", "电网"], slots=[], subagents=["geo_researcher"]))
    reg.register(dom_mod.DomainPack(
        id="ops", name="行政", description="采购报销",
        trigger_keywords=["采购", "报销"], semantic_hints=["预算"], slots=[],
        subagents=["rag_researcher"]))
    return RouterAgent(reg)


def test_multi_domain_when_close():
    r = _router_with_three()
    # 同时命中"充电/车"两个领域且置信接近 -> multi_domain，交给用户确认，不硬选
    d = r.route("我想买电动车，顺便看附近充电桩怎么布局")
    # 至少不应在两领域都强信号时擅自单领域执行
    assert d.mode in (MODE_MULTI, MODE_CLARIFY, MODE_AWAIT)
    if d.mode == MODE_MULTI:
        assert len(d.domains) >= 2 and d.needs_user is True


def test_no_domain_signal_clarifies_with_options():
    r = _router_with_three()
    d = r.route("今天心情不太好")
    assert d.mode == MODE_CLARIFY and d.needs_user is True
    assert d.options  # 提供领域选项而非空转


def test_router_never_auto_executes_high_risk():
    r = RouterAgent(dom_mod.load_default_registry())
    d = r.route("帮我下单买10台笔记本 预算5万")
    # 高风险：必须 await_confirmation，路由层绝不返回 activate 直接执行
    assert d.risk == "high"
    assert d.mode == MODE_AWAIT and d.needs_user is True
    # 选项必须让用户知情（含影响说明在 confirmation 模块；路由 options 含 confirm/cancel）
    assert {o["id"] for o in d.options} >= {"confirm", "cancel"}


def test_context_keeps_domain_across_turns():
    r = _router_with_three()
    d1 = r.route("采购一批办公用品")
    # 第二轮模糊追问，带上下文
    d2 = r.route("那报销怎么走", context={"domain": "ops", "known_slots": {}})
    assert d2.domains and d2.domains[0]["id"] == "ops"


def test_known_slots_not_reasked_via_router():
    r = RouterAgent(dom_mod.load_default_registry())
    d = r.route("我要采购笔记本电脑 预算8000元 数量3台")
    # 已提供 item/budget -> 不再作为 missing
    names = {m["name"] for m in d.missing_slots}
    assert "item" not in names and "budget" not in names
