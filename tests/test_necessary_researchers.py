"""问题4：Coordinator 只选必要研究员 + 已确认用户条件进入研究请求。"""
from __future__ import annotations

import pytest

from longflow import config as cfg_mod
from longflow import domains as dom_mod
from longflow import subagents as sa
from longflow.coordinator import coordinate


def _pack(*, subagents, domain=None, output_schema=None):
    return dom_mod.DomainPack(
        id="tst", name="tst", description="", trigger_keywords=["t"],
        semantic_hints=[], slots=[], subagents=subagents,
        scenario_cfg={"domain": domain or {}},
        output_schema=output_schema or {})


def _research_subagents(graph):
    return [n.subagent for n in graph.nodes if n.kind == "research"]


def test_simple_query_runs_one_researcher():
    # 三个研究员 + 无分支/无对比字段 -> 简单查询只跑 1 个
    pack = _pack(subagents=[sa.SA_WEB_RESEARCHER, sa.SA_FORUM_RESEARCHER,
                            sa.SA_OFFICIAL_RESEARCHER])
    g = coordinate(pack, "出差住宿标准", {})
    subs = _research_subagents(g)
    assert len(subs) == 1
    assert subs == [sa.SA_WEB_RESEARCHER]


def test_knowledge_query_prefers_rag():
    pack = _pack(subagents=[sa.SA_RAG_RESEARCHER, sa.SA_OFFICIAL_RESEARCHER,
                            sa.SA_WEB_RESEARCHER])
    g = coordinate(pack, "公司开会需提前多久订会议室", {})
    subs = _research_subagents(g)
    assert subs == [sa.SA_RAG_RESEARCHER]


def test_multi_source_runs_declared_researchers():
    # 汽车包：带 branch_on + 对比字段 -> 多来源，跑全部声明研究员（不含未声明的）
    reg = dom_mod.load_default_registry()
    car = reg.get("car_research")
    g = coordinate(car, "10万以内家用车", {"budget": 100000, "energy_type": "全部比较"})
    subs = set(_research_subagents(g))
    assert {sa.SA_OFFICIAL_RESEARCHER, sa.SA_WEB_RESEARCHER, sa.SA_FORUM_RESEARCHER} <= subs
    assert sa.SA_GEO_RESEARCHER not in subs


def test_geo_activated_only_with_location():
    # 多来源包 + GEO 在声明内：无地点不启动 GEO，有地点才启动
    pack = _pack(subagents=[sa.SA_GEO_RESEARCHER, sa.SA_OFFICIAL_RESEARCHER],
                 domain={"comparison_fields": ["distance_km"]})
    g_noloc = coordinate(pack, "附近咖啡厅", {"category": "coffee"})
    assert sa.SA_GEO_RESEARCHER not in _research_subagents(g_noloc)
    g_loc = coordinate(pack, "附近咖啡厅", {"location": "中关村", "category": "coffee"})
    assert sa.SA_GEO_RESEARCHER in _research_subagents(g_loc)


def test_geo_only_pack_always_geo():
    pack = _pack(subagents=[sa.SA_GEO_RESEARCHER],
                 domain={"comparison_fields": ["distance_km"]})
    g = coordinate(pack, "附近餐厅", {"location": "望京"})
    assert _research_subagents(g) == [sa.SA_GEO_RESEARCHER]


def test_user_conditions_enter_research_request():
    pack = _pack(subagents=[sa.SA_RAG_RESEARCHER, sa.SA_OFFICIAL_RESEARCHER,
                            sa.SA_WEB_RESEARCHER],
                 domain={"comparison_fields": ["price"]})
    g = coordinate(pack, "推荐通勤车", {"budget": 100000, "time_range": "2024-2025",
                                          "preference": "省油"})
    req = next(n.request for n in g.nodes if n.subagent == sa.SA_WEB_RESEARCHER)
    q = req["query"]
    assert "预算约100000" in q
    assert "省油" in q
    assert req.get("time_range") == "2024-2025"
    # 缺失条件不出现
    assert "地点" not in q