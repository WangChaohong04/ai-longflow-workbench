"""P4：搜索 Provider（按 query 非首页）、论坛结构化字段、单位标准化、透明评分。"""
from __future__ import annotations

import pytest

from longflow import subagents as sa
from longflow import evidence as E
from longflow.subagent_runner import (
    SubagentRunner, normalize_value, _transparent_scores)
from longflow.search_provider import SearchResult, NullSearchProvider


class _StubProvider:
    """返回按 query 检索的桩结果（证明用 query 检索结果页，非抓首页）。"""
    name = "stub"
    last_query = None
    def search(self, query, *, domains=None, time_range=None, fields=None,
               limit=8, kind="web"):
        type(self).last_query = query
        if kind == "forum":
            return [SearchResult(
                url="https://club.example.com/p/1", title="车主讨论：能耗",
                snippet="整体很省油 但有异响问题，略后悔", content="省油 省心 异响 问题",
                domain="club.example.com", published_at="2024-05-01",
                source_kind="forum", extra={"sample_size": 128, "replies": 128})]
        return [SearchResult(
            url="https://gov.example.com/policy/ev", title="新能源车政策",
            snippet="补贴与技术参数", content="纯电车型能耗 12 kWh/100km",
            domain="gov.example.com", published_at="2024-09-01", source_kind=kind)]


def test_web_research_uses_query_not_homepage():
    r = SubagentRunner(runtime=None, provider=_StubProvider())
    res = r.run(sa.SA_WEB_RESEARCHER,
                sa.SubagentRequest(query="10万以内 纯电 续航",
                                   allowed_domains=["example.com"]),
                task_id="t", root_id="t")
    assert res.ok
    assert _StubProvider.last_query == "10万以内 纯电 续航"
    assert res.findings["query"].startswith("10万")
    assert res.evidence[0]["source_url"].endswith("/policy/ev")  # 结果页，非域名根
    assert res.evidence[0]["layer"] == E.LAYER_FACT


def test_forum_keeps_structured_fields():
    r = SubagentRunner(runtime=None, provider=_StubProvider())
    res = r.run(sa.SA_FORUM_RESEARCHER,
                sa.SubagentRequest(query="某车 口碑", allowed_domains=["club.example.com"]),
                task_id="t", root_id="t")
    assert res.ok
    posts = res.findings["forum_posts"]
    assert posts and posts[0]["url"]
    assert posts[0]["published_at"] == "2024-05-01"
    assert posts[0]["sample_size"] == 128
    pos, neg = set(posts[0]["positive"]), set(posts[0]["negative"])
    assert {"省油", "省心"} & pos
    assert {"异响", "问题", "后悔"} & neg
    # 论坛观点层，不是事实
    assert res.evidence[0]["layer"] == E.LAYER_OPINION
    assert res.evidence[0]["source_type"] == E.SRC_FORUM


def test_null_provider_is_source_not_configured():
    r = SubagentRunner(runtime=None, provider=NullSearchProvider())
    res = r.run(sa.SA_WEB_RESEARCHER,
                sa.SubagentRequest(query="x", allowed_domains=["x.com"]),
                task_id="t", root_id="t")
    assert res.ok is False and res.error == "source_not_configured"
    assert res.evidence == []  # 不臆造


@pytest.mark.parametrize("value,unit,exp_val,exp_unit", [
    (1500, "m", 1.5, "km"),
    (3, "万", 30000, "元"),
    (500, "ml", 0.5, "L"),
    (2, "小时", 120, "分钟"),
    (1, "天", 1440, "分钟"),
])
def test_normalize_units(value, unit, exp_val, exp_unit):
    v, u = normalize_value(value, None, unit)
    assert u == exp_unit and abs(v - exp_val) < 1e-6


def test_energy_consumption_not_cross_converted():
    # L/100km 与 kWh/100km 是不同能量，不互相换算
    v, u = normalize_value(7.5, None, "L/100km")
    assert u == "L/100km" and v == 7.5
    v2, u2 = normalize_value(13, None, "kWh/100km")
    assert u2 == "kWh/100km" and v2 == 13


def test_weighted_score_transparent_not_fact():
    table = {
        "甲": {"price": {"value": 80, "unit": "元"},
               "fuel_consumption": {"value": 8, "unit": "L/100km"}},
        "乙": {"price": {"value": 100, "unit": "元"},
               "fuel_consumption": {"value": 5, "unit": "L/100km"}},
    }
    ranked, note = _transparent_scores(table, {"price": 0.6, "fuel_consumption": 0.4})
    # 越便宜越省油得分越高；甲便宜，乙省油 -> 甲价格权重高应胜出
    assert ranked[0]["entity"] == "甲"
    assert "非客观事实" in note or "不是客观事实" in note
    # 评分行带逐维归一，透明可核
    assert "dimension_norm" in ranked[0]


def test_comparison_returns_weighted_scores_when_weights():
    r = SubagentRunner(runtime=None)
    records = [
        E.make_record(entity="甲", field="price", value=80, unit="元").to_dict(),
        E.make_record(entity="乙", field="price", value=100, unit="元").to_dict(),
    ]
    req = sa.SubagentRequest(query="对比", required_fields=["price"],
                             extra={"weights": {"price": 1}})
    res = r.run(sa.SA_COMPARISON, req, task_id="t", root_id="t",
                input_records=records)
    assert res.ok
    assert res.findings.get("weighted_scores")
    assert any("不是客观事实" in x for x in res.limitations)
