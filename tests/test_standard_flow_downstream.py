"""问题2：标准化结果真正传给下游（核验/对比），保留溯源与冲突语义。

验收：
- 1500 米 与 1.5 公里 在同一实体/grid 中统一为 1.5 km；
- 相同实体别名（书写差异）对齐到同一规范实体；
- 冲突值不被覆盖（all_values 全保留）；
- 缺失字段不补零（不在表格中出现）；
- 原始证据可追溯：证据 id / 来源 / 版本 / 转换记录保留。
"""
from __future__ import annotations

import pytest

from longflow import evidence as E
from longflow.subagent_runner import SubagentRunner, sa, normalize_value

NORM = sa.SA_NORMALIZER
VERIFY = sa.SA_EVIDENCE_VERIFIER
CMP = sa.SA_COMPARISON


def _rec(entity, field, value, unit="", src="", layer=E.LAYER_FACT, version=""):
    return E.make_record(entity=entity, field=field, value=value, unit=unit,
                         source_title=src, source_url="https://src/" + (src or "x"),
                         layer=layer, source_version=version).to_dict()


@pytest.fixture
def runner():
    return SubagentRunner(runtime=None)


def test_normalize_unifies_distance_units(runner):
    records = [
        _rec("A", "distance", 1500, unit="m", src="s1"),
        _rec("A", "distance", 1.5, unit="公里", src="s2"),
    ]
    res = runner.run(NORM, sa.SubagentRequest(), task_id="t", root_id="t",
                     input_records=records)
    assert res.ok
    by_canon = {}
    for n in res.normalized:
        by_canon.setdefault(n["entity_canonical"], []).append(n)
    (group,) = by_canon.values()
    vals = {round(v["value"], 6) for v in group}
    assert vals == {1.5}, group
    assert {v["unit"] for v in group} == {"km"}
    # 转换记录与证据 id 保留
    assert any(v["conversion"] and v["id"].startswith("e_") for v in group)


def test_entity_alias_aligns(runner):
    records = [
        _rec("B  X2023", "price", 100, unit="元", src="p1"),
        _rec("BX2023", "price", 110, unit="元", src="p2"),
    ]
    res = runner.run(NORM, sa.SubagentRequest(), task_id="t", root_id="t",
                     input_records=records)
    canons = {n["entity_canonical"] for n in res.normalized}
    assert len(canons) == 1, canons


def test_compare_normalizes_and_traces(runner):
    # 已标准化输入：同一规范实体，1500米 vs 1.5公里 + 别名行
    normalized = runner.run(
        NORM, sa.SubagentRequest(), task_id="t", root_id="t", input_records=[
            _rec("Caro", "range", 1500, unit="m", src="a1", version="2024"),
            _rec("CARO", "range", 1.5, unit="公里", src="a2", version="2025"),
        ]).normalized
    res = runner.run(CMP, sa.SubagentRequest(query="对比", required_fields=["range"]),
                     task_id="t", root_id="t", input_records=normalized)
    assert res.ok
    assert len(res.findings["comparison"]) == 1, res.findings["comparison"]  # 别名已合并
    row = res.findings["comparison"][0]
    cell = row["attributes"]["range"]
    # 两个值都保留（冲突/多来源），单位统一 km，证据 id/来源/版本可追溯
    assert {round(v, 6) for v in cell["all_values"]} == {1.5}
    assert set(cell["all_units"]) == {"km"}
    assert len(cell["evidence_ids"]) == 2
    all_sources = " ".join(cell["sources"])
    assert "a1" in all_sources and "a2" in all_sources, cell["sources"]


def test_conflict_not_overwritten_missing_not_zero(runner):
    normalized = runner.run(
        NORM, sa.SubagentRequest(), task_id="t", root_id="t", input_records=[
            _rec("X", "price", 100, unit="元", src="v1", version="2023"),
            _rec("X", "price", 500, unit="元", src="v2", version="2024"),
        ]).normalized
    res = runner.run(CMP, sa.SubagentRequest(query="对比", required_fields=["price", "range"]),
                     task_id="t", root_id="t", input_records=normalized)
    assert res.ok
    row = res.findings["comparison"][0]
    cell = row["attributes"]["price"]
    assert set(cell["all_values"]) == {100, 500}  # 冲突不覆盖
    assert cell["value"] == 500  # 展示最近值，历史全保留
    assert "range" not in row["attributes"]  # 缺失字段不补零、不出现


def test_verifier_consumes_normalized(runner):
    # 500 元 vs 400 元 同一实体+字段 -> 冲突被核验检出（不自动裁决）
    normalized = runner.run(
        NORM, sa.SubagentRequest(), task_id="t", root_id="t", input_records=[
            _rec("住宿", "限额", 400, unit="元", src="doc2023"),
            _rec("住宿", "限额", 500, unit="元", src="doc2024"),
        ]).normalized
    res = runner.run(VERIFY, sa.SubagentRequest(), task_id="t", root_id="t",
                     input_records=normalized)
    assert res.ok
    assert len(res.findings["conflicts"]) >= 1