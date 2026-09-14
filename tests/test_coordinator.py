"""P3 Coordinator：动态任务图、汽车四分支、缺槽位澄清、越权/有界/无环校验。"""
from __future__ import annotations

import pytest

from longflow import config as cfg_mod
from longflow import domains as dom_mod
from longflow import subagents as sa
from longflow.coordinator import Coordinator, coordinate, PlanError, is_all_branches


@pytest.fixture(scope="module")
def car_pack():
    reg = dom_mod.load_default_registry()
    pack = reg.get("car_research")
    assert pack is not None, "需要 config/scenarios/car_research.yaml"
    return pack


def test_car_missing_energy_clarifies(car_pack):
    # 只有预算，缺能源类型 -> 不产图，返回澄清
    missing = [{"name": "energy_type", "prompt": "能源类型？"}]
    g = coordinate(car_pack, "10万以内家用车推荐", {"budget": 100000},
                   missing_slots=missing)
    assert g.nodes == [] and any(m["name"] == "energy_type" for m in g.clarify)


def test_car_all_compare_builds_four_branches(car_pack):
    g = coordinate(car_pack, "10万以内家用车推荐",
                   {"budget": 100000, "energy_type": "全部比较"})
    branch_ids = {b["id"] for b in g.branches}
    assert branch_ids == {"icev", "bev", "phev", "erev"}
    # 每个研究型 subagent × 4 分支都有研究节点，且并行
    research_nodes = [n for n in g.nodes if n.kind == "research"]
    assert len({n.branch for n in research_nodes}) == 4
    assert all(n.parallel_group == "research" for n in research_nodes)
    # 汇聚节点存在且不属于任一分支
    kinds = {n.kind: n for n in g.nodes}
    assert {"normalize", "verify", "compare"} <= set(kinds)
    # compare 依赖 verify；汇聚节点依赖研究结果
    assert "verify" in kinds["compare"].depends_on
    assert kinds["normalize"].depends_on  # 依赖研究节点
    # 所有 subagent 都在汽车包白名单内
    assert all(n.subagent in car_pack.subagents for n in g.nodes)


def test_car_single_energy_one_branch(car_pack):
    g = coordinate(car_pack, "10万以内家用车", {"budget": 100000, "energy_type": "bev"})
    assert {b["id"] for b in g.branches} == {"bev"}
    # 单分支节点 branch=bev
    assert all(n.branch == "bev" for n in g.nodes if n.kind == "research")


def test_comparison_uses_confirmed_criteria(car_pack):
    g = coordinate(car_pack, "10万以内家用车 全部比较",
                   {"budget": 100000, "energy_type": "all"})
    cmp_node = next(n for n in g.nodes if n.subagent == sa.SA_COMPARISON)
    crit = cmp_node.request["required_fields"]
    # 来自领域包 comparison_fields，含价格/能耗，不含主观推荐
    assert "price" in crit and "fuel_consumption" in crit
    assert not any("推荐" in c for c in crit)


def test_coordinator_rejects_unregistered_subagent():
    pack = dom_mod.DomainPack(
        id="x", name="x", description="x", trigger_keywords=["x"],
        semantic_hints=[], slots=[], subagents=["rag_researcher"])
    # 手工注入一个非法节点再校验
    from longflow.coordinator import PlanNode
    c = Coordinator(pack)
    g = c.plan("x", {})
    g.nodes.append(PlanNode(key="bad", subagent="ghost_agent", kind="research",
                            label="x", request={}))
    with pytest.raises(PlanError):
        c.validate(g)


def test_coordinator_rejects_out_of_whitelist():
    pack = dom_mod.DomainPack(
        id="x", name="x", description="x", trigger_keywords=["x"],
        semantic_hints=[], slots=[], subagents=["rag_researcher"])
    from longflow.coordinator import PlanNode
    c = Coordinator(pack)
    g = c.plan("x", {})
    g.nodes.append(PlanNode(key="bad", subagent=sa.SA_GEO_RESEARCHER,
                            kind="research", label="x", request={}))
    with pytest.raises(PlanError):
        c.validate(g)


def test_graph_dag_and_bounded(car_pack):
    g = coordinate(car_pack, "家用车 全部比较",
                   {"budget": 100000, "energy_type": "全部"})
    from longflow.coordinator import MAX_NODES
    assert len(g.nodes) <= 24
    keys = [n.key for n in g.nodes]
    pos = {k: i for i, k in enumerate(keys)}
    for n in g.nodes:
        assert all(pos[d] < pos[n.key] for d in n.depends_on)


def test_is_all_branches_values():
    for v in ["全部比较", "都可以", "all", "不限", "所有"]:
        assert is_all_branches(v)
    assert not is_all_branches("bev")
    assert not is_all_branches(None)
