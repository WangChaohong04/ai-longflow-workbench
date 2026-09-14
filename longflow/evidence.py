"""统一证据结构 EvidenceRecord 与证据分层。

所有网页/论坛/文件/API/RAG/GEO 结果在进入汇总前都归一为 EvidenceRecord，
并对每条论断标注其"认识层级"：已确认事实 / 用户或论坛观点 / 模型推断 /
推荐意见 / 未确认内容 / 数据冲突 / 数据缺失。模拟数据必须显式标 source_type=mock，
不得伪装成真实来源。
"""
from __future__ import annotations

import dataclasses
from typing import Any

# 来源类型
SRC_OFFICIAL = "official"
SRC_FORUM = "forum"
SRC_DOCUMENT = "document"
SRC_API = "api"
SRC_MOCK = "mock"
SOURCE_TYPES = {SRC_OFFICIAL, SRC_FORUM, SRC_DOCUMENT, SRC_API, SRC_MOCK}

# 证据质量
QUALITY_HIGH = "high"
QUALITY_MEDIUM = "medium"
QUALITY_LOW = "low"

# 认识层级（论断性质）
LAYER_FACT = "confirmed_fact"       # 已确认事实（有权威来源支撑）
LAYER_OPINION = "opinion"           # 用户/论坛观点（主观，非事实）
LAYER_INFERENCE = "model_inference"  # 模型推断（无直接证据）
LAYER_RECOMMENDATION = "recommendation"  # 推荐意见（价值判断，须用户定夺）
LAYER_UNCONFIRMED = "unconfirmed"    # 未确认内容
LAYER_CONFLICT = "conflict"          # 数据冲突
LAYER_MISSING = "missing"            # 数据缺失

LAYER_LABELS = {
    LAYER_FACT: "已确认",
    LAYER_OPINION: "观点",
    LAYER_INFERENCE: "推断",
    LAYER_RECOMMENDATION: "推荐",
    LAYER_UNCONFIRMED: "未确认",
    LAYER_CONFLICT: "冲突",
    LAYER_MISSING: "缺失",
}


@dataclasses.dataclass
class EvidenceRecord:
    entity: str = ""                 # 对象/实体名
    field: str = ""                  # 属性字段（如 price/distance/rating）
    value: Any = None                # 值
    unit: str = ""                   # 单位
    source_type: str = SRC_DOCUMENT  # official|forum|document|api|mock
    source_url: str = ""
    source_title: str = ""
    source_version: str = ""
    collected_at: str = ""
    evidence_text: str = ""          # 支撑原文片段
    quality: str = QUALITY_MEDIUM    # high|medium|low
    limitations: list[str] = dataclasses.field(default_factory=list)
    layer: str = LAYER_FACT          # 认识层级，见 LAYER_*
    page: str | None = None          # 文件页码（文件来源）
    effective_at: str | None = None  # 生效时间
    expires_at: str | None = None    # 失效时间
    id: str = ""                     # 证据 ID（标准化后生成，供下游追溯）
    conversion: str = ""             # 单位/取值转换记录（原文→规范）

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "EvidenceRecord":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


def make_record(**kw) -> EvidenceRecord:
    """构造证据；强制 source_type 合法、mock 显式。"""
    st = kw.get("source_type", SRC_DOCUMENT)
    if st not in SOURCE_TYPES:
        raise ValueError(f"非法 source_type: {st}")
    q = kw.get("quality", QUALITY_MEDIUM)
    if q not in {QUALITY_HIGH, QUALITY_MEDIUM, QUALITY_LOW}:
        raise ValueError(f"非法 quality: {q}")
    return EvidenceRecord(**kw)


def from_chunk(chunk: dict, *, layer: str = LAYER_FACT) -> EvidenceRecord:
    """把 RAG chunk 归一为 EvidenceRecord。"""
    src = chunk.get("source_type")
    if not src:
        # 本地样例/知识库文档默认 document；mock 数据在 doc_name/source 标注
        src = SRC_MOCK if "mock" in (chunk.get("doc_name", "") + str(chunk.get("source", ""))).lower() \
            else SRC_DOCUMENT
    return make_record(
        entity=chunk.get("doc_name") or chunk.get("entity", ""),
        field=chunk.get("section") or chunk.get("field", ""),
        value=None,
        source_type=src,
        source_title=chunk.get("doc_name", ""),
        source_version=chunk.get("version", ""),
        collected_at=chunk.get("updated_at") or chunk.get("collected_at", ""),
        evidence_text=chunk.get("text", ""),
        quality=QUALITY_HIGH if src == SRC_OFFICIAL else QUALITY_MEDIUM,
        layer=layer,
        effective_at=chunk.get("effective_at"),
        expires_at=chunk.get("expires_at"),
    )


def layerize(records: list[EvidenceRecord]) -> dict[str, list[dict]]:
    """按认识层级分组，供前端"已确认/推断/未确认/无法回答"分层展示。"""
    groups: dict[str, list[dict]] = {}
    for r in records:
        groups.setdefault(r.layer, []).append(r.to_dict())
    return groups


def detect_conflicts(records: list[EvidenceRecord]) -> list[dict]:
    """同一 entity+field 出现不同 value 且都非观点/缺失 -> 标记冲突（不自动裁决）。"""
    bucket: dict[tuple[str, str], dict[Any, list[EvidenceRecord]]] = {}
    for r in records:
        if r.layer in (LAYER_OPINION, LAYER_MISSING, LAYER_INFERENCE, LAYER_RECOMMENDATION):
            continue
        if r.value is None:
            continue
        bucket.setdefault((r.entity, r.field), {}).setdefault(_norm(r.value), []).append(r)
    conflicts = []
    for (entity, field), values in bucket.items():
        if len(values) > 1:
            recs = [r for lst in values.values() for r in lst]
            conflicts.append({
                "entity": entity, "field": field,
                "values": [r.value for r in recs],
                "sources": [r.source_title or r.source_url for r in recs],
                "versions": [r.source_version for r in recs],
            })
    return conflicts


def _norm(v: Any) -> Any:
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v).strip()


def answer_layers(records: list[EvidenceRecord]) -> dict:
    """把证据按"结论可信度"汇总成四桶，供前端分层展示：
    confirmed（已确认事实）/ inferred（推断）/ unconfirmed（未确认/观点）/ unanswerable（缺失/冲突）。"""
    groups = layerize(records)
    confirmed = groups.get(LAYER_FACT, [])
    inferred = groups.get(LAYER_INFERENCE, [])
    unconfirmed = groups.get(LAYER_UNCONFIRMED, []) + groups.get(LAYER_OPINION, [])         + groups.get(LAYER_RECOMMENDATION, [])
    conflicts = detect_conflicts(records)
    unanswerable = groups.get(LAYER_MISSING, []) + groups.get(LAYER_CONFLICT, [])
    return {
        "confirmed": confirmed,
        "inferred": inferred,
        "unconfirmed": unconfirmed,
        "unanswerable": unanswerable,
        "conflicts": conflicts,
        "counts": {
            "confirmed": len(confirmed),
            "inferred": len(inferred),
            "unconfirmed": len(unconfirmed),
            "unanswerable": len(unanswerable),
        },
    }
