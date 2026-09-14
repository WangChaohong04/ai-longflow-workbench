"""批次1：总路由 Agent / Domain Registry / 固定能力 Subagent 注册。"""
from __future__ import annotations

import pytest

from longflow import domains as dom_mod
from longflow import subagents as sa
from longflow.router import RouterAgent, MODE_ACTIVATE, MODE_CLARIFY, MODE_AWAIT, MODE_HANDOFF


@pytest.fixture()
def router():
    return RouterAgent(dom_mod.load_default_registry())


def test_domain_registry_loads_scenario_packs():
    reg = dom_mod.load_default_registry()
    ids = {p.id for p in reg.all()}
    assert {"team_ops", "geo_site"} <= ids
    # 领域包暴露 subagent 白名单与语义提示
    geo = reg.get("geo_site")
    assert sa.SA_GEO_RESEARCHER in geo.subagents
    assert geo.semantic_hints


def test_duplicate_domain_registration_rejected():
    reg = dom_mod.DomainRegistry()
    pack = dom_mod.DomainPack(id="x", name="x", description="", trigger_keywords=["a"],
                              semantic_hints=[], slots=[], subagents=[])
    reg.register(pack)
    with pytest.raises(ValueError):
        reg.register(pack)


def test_router_activates_knowledge_domain(router):
    d = router.route("出差住宿标准是多少")
    assert d.mode == MODE_ACTIVATE
    assert d.domains[0]["id"] == "team_ops"
    assert d.domains[0]["confidence"] >= 0.6
    assert d.needs_user is False


def test_router_high_risk_awaits_confirmation(router):
    d = router.route("我要采购笔记本电脑 预算8000元")
    assert d.risk == "high"
    assert d.mode == MODE_AWAIT and d.needs_user is True
    # 确认选项包含继续/调整/取消
    assert {o["id"] for o in d.options} >= {"confirm", "cancel"}


def test_router_readonly_comparison_is_not_high_risk(router):
    # 只读对比/查询不应判为高风险
    d = router.route("帮我对比一下差旅住宿和报销规则")
    assert d.risk != "high"
    assert d.mode == MODE_ACTIVATE


def test_router_geo_domain(router):
    d = router.route("在望京5公里内找适合办公的咖啡馆")
    assert d.mode == MODE_ACTIVATE
    assert d.domains[0]["id"] == "geo_site"
    assert d.known_slots.get("radius_km") == 5
    assert d.known_slots.get("location") == "望京"


def test_router_missing_slots_clarifies(router):
    d = router.route("我要采购")
    assert d.mode == MODE_CLARIFY and d.needs_user is True
    names = {m["name"] for m in d.missing_slots}
    assert {"item", "budget"} & names


def test_router_low_confidence_asks_domain(router):
    d = router.route("随便聊聊今天的心情")
    assert d.mode == MODE_CLARIFY
    assert d.domains == []  # 无领域命中
    assert d.options  # 提供可选领域


def test_router_handoff(router):
    d = router.route("帮我转人工客服")
    assert d.mode == MODE_HANDOFF and d.needs_user is True


def test_router_uses_context_for_continuity():
    reg = dom_mod.load_default_registry()
    r = RouterAgent(reg)
    # 第二轮模糊消息，带上下文已知领域 -> 仍归到 team_ops
    d = r.route("那报销呢", context={"domain": "team_ops", "known_slots": {}})
    assert d.domains and d.domains[0]["id"] == "team_ops"


# ---- 固定能力 Subagent 机制 ----

def test_subagent_whitelist_blocks_unauthorized_tool():
    # rag_researcher 只能调 kb_search，不能调 make_purchase
    with pytest.raises(PermissionError):
        sa.assert_tool_allowed(sa.SA_RAG_RESEARCHER, "make_purchase")
    # 允许的工具通过
    sa.assert_tool_allowed(sa.SA_RAG_RESEARCHER, "kb_search")
    sa.assert_tool_allowed(sa.SA_GEO_RESEARCHER, "geo_radius_search")


def test_subagent_unknown_rejected():
    with pytest.raises(PermissionError):
        sa.assert_tool_allowed("not_a_real_subagent", "kb_search")


def test_subagent_specs_never_return_decision():
    for spec in sa.all_specs():
        assert spec.returns_decision is False


def test_subagent_request_validation():
    req = sa.SubagentRequest(query="")
    missing = sa.validate_request(sa.SA_WEB_RESEARCHER, req)
    assert "query" in missing
    req2 = sa.SubagentRequest(query="充电桩")
    assert sa.validate_request(sa.SA_WEB_RESEARCHER, req2) == []


def test_duplicate_subagent_registration_rejected():
    with pytest.raises(ValueError):
        sa.register_subagent(sa.SubagentSpec(
            sa.SA_RAG_RESEARCHER, "x", "x", ["kb_search"], [], []))


def test_domain_rejecting_unknown_subagent():
    reg = dom_mod.DomainRegistry()
    bad = dom_mod.DomainPack(id="b", name="b", description="", trigger_keywords=["z"],
                             semantic_hints=[], slots=[], subagents=["ghost_subagent"])
    with pytest.raises(ValueError):
        reg.register(bad)
