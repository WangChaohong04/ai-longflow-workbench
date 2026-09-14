"""批次3：统一证据结构 EvidenceRecord、来源类型、证据分层与冲突检测。"""
from __future__ import annotations

import pytest

from longflow import evidence as E


def test_record_source_type_validation():
    r = E.make_record(entity="x", field="price", value=10, source_type=E.SRC_OFFICIAL)
    assert r.source_type == "official" and r.layer == E.LAYER_FACT
    with pytest.raises(ValueError):
        E.make_record(source_type="fabricated")
    with pytest.raises(ValueError):
        E.make_record(quality="perfect")


def test_mock_must_be_explicit():
    r = E.make_record(entity="天气", field="temp", value=20, source_type=E.SRC_MOCK,
                      layer=E.LAYER_FACT)
    assert r.source_type == "mock"
    d = r.to_dict()
    # mock 来源在结构中显式可见，不得伪装成 official/api
    assert d["source_type"] == "mock"


def test_from_chunk_normalizes_rag():
    chunk = {"chunk_id": "c1", "doc_name": "procurement.md", "section": "审批",
             "text": "超过5000元须总监审批", "version": "2024"}
    rec = E.from_chunk(chunk)
    assert rec.source_type == E.SRC_DOCUMENT
    assert rec.source_title == "procurement.md" and rec.source_version == "2024"
    assert "5000" in rec.evidence_text


def test_from_chunk_marks_mock_doc():
    chunk = {"doc_name": "weather_mock.json", "text": "晴"}
    rec = E.from_chunk(chunk)
    assert rec.source_type == E.SRC_MOCK


def test_layerize_groups_by_layer():
    recs = [
        E.make_record(entity="a", field="f", value=1, layer=E.LAYER_FACT),
        E.make_record(entity="a", field="g", value="好", layer=E.LAYER_OPINION),
        E.make_record(entity="a", field="h", layer=E.LAYER_UNCONFIRMED),
    ]
    groups = E.layerize(recs)
    assert E.LAYER_FACT in groups and E.LAYER_OPINION in groups and E.LAYER_UNCONFIRMED in groups
    # 认识层级标签完备
    for layer in (E.LAYER_FACT, E.LAYER_OPINION, E.LAYER_INFERENCE,
                  E.LAYER_RECOMMENDATION, E.LAYER_UNCONFIRMED, E.LAYER_CONFLICT, E.LAYER_MISSING):
        assert layer in E.LAYER_LABELS


def test_detect_conflicts_same_entity_field_diff_value():
    recs = [
        E.make_record(entity="住宿标准", field="限额", value=400, unit="元",
                      source_title="travel_2023.md", source_version="2023"),
        E.make_record(entity="住宿标准", field="限额", value=500, unit="元",
                      source_title="travel_2024.md", source_version="2024"),
    ]
    conflicts = E.detect_conflicts(recs)
    assert len(conflicts) == 1
    assert set(conflicts[0]["values"]) == {400.0, 500.0}


def test_opinions_not_treated_as_conflicts():
    recs = [
        E.make_record(entity="场地", field="评价", value="安静", layer=E.LAYER_OPINION),
        E.make_record(entity="场地", field="评价", value="吵", layer=E.LAYER_OPINION),
    ]
    # 观点分歧不是事实冲突
    assert E.detect_conflicts(recs) == []


def test_missing_and_inference_separated():
    recs = [
        E.make_record(entity="x", field="price", layer=E.LAYER_MISSING),
        E.make_record(entity="x", field="distance", value=1.2, layer=E.LAYER_FACT),
    ]
    groups = E.layerize(recs)
    assert E.LAYER_MISSING in groups and E.LAYER_FACT in groups
