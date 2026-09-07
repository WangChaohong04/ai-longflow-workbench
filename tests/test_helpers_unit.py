"""辅助函数单元测试（不依赖后端实现，任何时候都应通过）。

- canonical_json：SPEC §1 幂等键/批准绑定的 canonical 比对基础；
- haversine：SPEC §8 距离必须真实计算（测试独立复算，不信任插件输出）；
- 词重叠核验：SPEC §4 出口闸门引用核验原理。
"""
from __future__ import annotations

import math

import conftest as cf


def test_canonical_json_stable():
    a = cf.canonical_json({"b": 1, "a": [1, 2, {"x": "中"}]})
    b = cf.canonical_json({"a": [1, 2, {"x": "中"}], "b": 1})
    assert a == b
    # 键排序 + 无多余空白（canonical）
    assert a.index('"a"') < a.index('"b"')
    assert ": " not in a and ", " not in a
    # 中文不转义
    assert "中" in a


def test_haversine_known_distance():
    # 北京天安门 (116.397, 39.908) → 中关村 (116.310, 39.984)：约 11~12 km
    d = cf.haversine_km(116.397, 39.908, 116.310, 39.984)
    assert 10.0 < d < 13.0, f"天安门-中关村距离异常: {d}"
    # 同点距离为 0
    assert cf.haversine_km(116.0, 40.0, 116.0, 40.0) == 0.0
    # 1 度纬度 ≈ 111 km
    d_lat = cf.haversine_km(116.0, 40.0, 116.0, 41.0)
    assert abs(d_lat - 111.19) < 2.0


def test_overlap_ratio_chinese():
    claim = "采购金额超过五万元须经分管副总裁审批"
    source = "第三条 采购审批规则：单笔采购金额超过人民币五万元的，须报分管副总裁审批后方可执行。"
    r = cf.text_overlap_ratio(claim, source)
    assert r >= 0.6, f"支撑性文本重叠应较高: {r}"
    r2 = cf.text_overlap_ratio("公司宠物保险报销比例为百分之八十", source)
    assert r2 < 0.4, f"无关论断重叠应较低: {r2}"


def test_overlap_ratio_english():
    claim = "purchase approval threshold amount"
    source = "The purchase approval rule sets a threshold amount of 50000 CNY."
    assert cf.text_overlap_ratio(claim, source) >= 0.7
    assert cf.text_overlap_ratio("pet insurance reimbursement", source) < 0.3


def test_cjk_bigram_tokenizer():
    toks = cf._content_tokens("采购审批规则")
    # bigram：采购/购审/审批/批规/规则
    assert {"采购", "审批", "规则"} <= toks
