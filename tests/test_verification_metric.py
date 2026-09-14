"""P5：数字核验结合指标/实体/时间版本——住宿500不能支持采购预算500。"""
from __future__ import annotations

from longflow.verification import verify_answer, VERIFIED, PARTIAL, FAILED, INSUFFICIENT


def _chunks(texts):
    return [{"chunk_id": f"c{i}", "text": t} for i, t in enumerate(texts)]


def test_same_amount_different_metric_not_support():
    # 证据是"住宿上限 500 元"，答案却声称"采购预算 500 元"
    chunks = _chunks(["# 差旅住宿\n员工出差住宿上限 500 元/晚。"])
    res = verify_answer("采购预算为 500 元。", ["c0"], chunks)
    # 不得判 verified；500 元同值但业务指标不同 -> unconfirmed/partial（不是失败也不是通过）
    assert res.verdict != VERIFIED
    statuses = {c["status"] for c in res.claims if c["kind"] == "number"}
    assert "supported" not in statuses


def test_same_metric_same_amount_supports():
    chunks = _chunks(["员工出差住宿上限 500 元/晚。"])
    res = verify_answer("出差住宿上限为 500 元。", ["c0"], chunks)
    assert res.verdict == VERIFIED
    assert any(c["status"] == "supported" for c in res.claims if c["kind"] == "number")


def test_version_year_mismatch_not_support():
    # 2024 年标准不支持 2025 年的同额论断（时间冲突 -> 不支撑）
    chunks = _chunks(["2024 年差旅住宿上限 500 元。"])
    res = verify_answer("2025 年住宿上限为 500 元。", ["c0"], chunks)
    assert res.verdict != VERIFIED
    assert all(c["status"] != "supported" for c in res.claims if c["kind"] == "number")


def test_same_metric_different_value_contradicted():
    chunks = _chunks(["员工出差住宿上限 500 元/晚。"])
    res = verify_answer("住宿上限是 300 元。", ["c0"], chunks)
    assert res.verdict == FAILED
    assert any(c["status"] == "contradicted" for c in res.claims if c["kind"] == "number")


def test_no_citation_factual_is_insufficient():
    # 有事实数字但零引用 -> 证据不足，不通过
    res = verify_answer("住宿上限是 500 元。", [], _chunks([]))
    assert res.verdict in (INSUFFICIENT, FAILED)
    assert res.verdict != VERIFIED


def test_plain_fact_without_deterministic_check_not_auto_verified():
    # 无数值/极性的普通事实：确定性核验无法证明 -> 不因有引用就 verified
    chunks = _chunks(["某供应商提供办公耗材。"])
    res = verify_answer("某供应商提供办公耗材且送货最快。", ["c0"], chunks)
    assert res.verdict in (PARTIAL, INSUFFICIENT)
    assert res.verdict != VERIFIED
