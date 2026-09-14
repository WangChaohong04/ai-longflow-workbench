"""固定能力 Subagent 的执行器（实际可运行；只返回证据，不做最终决策）。

SubagentRunner 把 SubagentRequest 落到工具调用（受白名单/权限/超时约束），
并把工具结果**归一为统一 EvidenceRecord**。领域主 Agent 只传任务参数，
subagent 不扩大工具范围、不替用户做决策、不触发高风险副作用。

纯计算型 subagent（normalizer / evidence_verifier / comparison_agent）不调用工具，
只对已有证据做标准化/核验/对照，输出结构化结果。
"""
from __future__ import annotations

import dataclasses
from typing import Any
import re

from . import evidence as E
from . import subagents as sa
from . import permissions, db


class _FakeTask:
    """供工具运行时使用的最小任务句柄（subagent 不绑定真实 task 行）。"""
    def __init__(self, task_id: str, root_id: str, role: str, scenario: str, slots: dict | None = None):
        self.id = task_id
        self.root_id = root_id
        self.agent_role = role
        self.slots = {"__scenario__": scenario, **(slots or {})}
        self.plan = {}
        self.objective = ""


def _rec(**kw) -> E.EvidenceRecord:
    return E.make_record(**kw)


class SubagentRunner:
    def __init__(self, runtime, provider=None, cfg=None):
        # runtime: longflow.tools.ToolRuntime（含 conn/registry/权限/超时）
        self.runtime = runtime
        # 可替换搜索 Provider（默认从 runtime 的 http 客户端构造；无后端=NullProvider）
        if provider is not None:
            self.provider = provider
        else:
            from .search_provider import default_provider
            http = getattr(runtime, "http", None)
            self.provider = default_provider(http, cfg)

    def run(self, subagent_id: str, req: sa.SubagentRequest, *,
            task_id: str, root_id: str, scenario: str = "",
            input_records: list[dict] | None = None) -> sa.SubagentResult:
        spec = sa.get_spec(subagent_id)
        if spec is None:
            return sa.SubagentResult(subagent=subagent_id, ok=False,
                                     error=f"未知 subagent: {subagent_id}", needs_user=True)
        missing = sa.validate_request(subagent_id, req)
        if missing:
            return sa.SubagentResult(
                subagent=subagent_id, ok=False, needs_user=True,
                question=f"执行 {subagent_id} 缺少必要参数：{', '.join(missing)}",
                error=f"missing_params:{missing}")

        try:
            if subagent_id in (sa.SA_RAG_RESEARCHER, sa.SA_FILE_RESEARCHER):
                return self._run_kb(spec, req, task_id, root_id, scenario, E.SRC_DOCUMENT)
            if subagent_id == sa.SA_OFFICIAL_RESEARCHER:
                return self._run_official(spec, req, task_id, root_id, scenario)
            if subagent_id in (sa.SA_WEB_RESEARCHER, sa.SA_FORUM_RESEARCHER):
                st = E.SRC_API if subagent_id == sa.SA_WEB_RESEARCHER else E.SRC_FORUM
                return self._run_web(spec, req, task_id, root_id, st)
            if subagent_id == sa.SA_GEO_RESEARCHER:
                return self._run_geo(spec, req, task_id, root_id, scenario)
            if subagent_id == sa.SA_NORMALIZER:
                return self._run_normalize(req, input_records or [])
            if subagent_id == sa.SA_EVIDENCE_VERIFIER:
                return self._run_verify(req, input_records or [])
            if subagent_id == sa.SA_COMPARISON:
                return self._run_compare(req, input_records or [])
        except permissions.PermissionDenied as exc:
            return sa.SubagentResult(subagent=subagent_id, ok=False, needs_user=True,
                                     question=f"权限不足，需要授权：{exc}", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - subagent 失败不伪装成功
            return sa.SubagentResult(subagent=subagent_id, ok=False, needs_user=False,
                                     error=f"subagent 执行异常: {str(exc)[:200]}")
        return sa.SubagentResult(subagent=subagent_id, ok=False, error="unhandled")

    # ---------- 工具调用封装（带白名单） ----------

    def _call_tool(self, spec, tool: str, args: dict, task_id: str, root_id: str,
                   role: str, scenario: str) -> dict:
        sa.assert_tool_allowed(spec.id, tool)  # 越权拦截
        task = _FakeTask(task_id, root_id, f"subagent:{spec.id}", scenario)
        return self.runtime.call(tool, args, task, reason=f"{spec.id} 调用 {tool}")

    # ---------- RAG / 文件检索 ----------

    def _run_kb(self, spec, req, tid, rid, scenario, source_type) -> sa.SubagentResult:
        out = self._call_tool(spec, "kb_search",
                              {"query": req.query, "top_k": req.max_sources,
                               "fields": req.required_fields or None},
                              tid, rid, spec.id, scenario)
        chunks = out.get("chunks", []) or []
        records = [E.from_chunk(c).to_dict() for c in chunks]
        if not records:
            return sa.SubagentResult(
                subagent=spec.id, ok=True, evidence=[], needs_user=False,
                limitations=["知识库未召回任何已激活资料"],
                findings={"count": 0, "stages": out.get("retrieval_stages", [])})
        return sa.SubagentResult(subagent=spec.id, ok=True, evidence=records,
                                 findings={"count": len(records),
                                           "stages": out.get("retrieval_stages", [])},
                                 limitations=["仅检索已导入并激活的知识，不含实时网页/价格/库存"])

    # ---------- 官方来源：优先知识库，缺目标站点时不臆造 ----------

    def _run_official(self, spec, req, tid, rid, scenario) -> sa.SubagentResult:
        if not req.target_sites and not req.allowed_domains:
            # 无外部站点配置：退回知识库官方资料
            return self._run_kb(spec, req, tid, rid, scenario, E.SRC_OFFICIAL)
        return self._run_web(spec, req, tid, rid, E.SRC_OFFICIAL)

    # ---------- 网页 / 论坛 ----------

    def _run_web(self, spec, req, tid, rid, source_type) -> sa.SubagentResult:
        domains = req.target_sites or req.allowed_domains
        kind = "forum" if source_type == E.SRC_FORUM else (
            "official" if source_type == E.SRC_OFFICIAL else "web")
        # Provider 未配置后端 -> 来源缺口（不阻塞整图、不臆造）
        if getattr(self.provider, "name", "null") == "null":
            return sa.SubagentResult(
                subagent=spec.id, ok=False, needs_user=False,
                error="source_not_configured",
                limitations=["未配置搜索后端/目标域名，跳过该外部来源（不臆造内容）"])
        try:
            results = self.provider.search(
                req.query, domains=domains or None, time_range=req.time_range,
                fields=req.required_fields or None, limit=req.max_sources, kind=kind)
        except Exception as exc:  # noqa: BLE001
            return sa.SubagentResult(subagent=spec.id, ok=False, needs_user=False,
                                     error=f"search_unreachable: {str(exc)[:200]}",
                                     limitations=["搜索后端不可达，该来源缺失"])
        if not results:
            return sa.SubagentResult(subagent=spec.id, ok=False, needs_user=False,
                                     error="source_unreachable",
                                     limitations=["按 query 未检索到任何结果（非抓首页）"])
        records = []
        forum_summaries = []
        for r in results:
            text = r.content or r.snippet
            if kind == "forum":
                rec = _rec(
                    entity=r.title, field="forum_opinion", value=None,
                    source_type=E.SRC_FORUM, source_url=r.url, source_title=r.title,
                    source_version=r.published_at, collected_at=db.now(),
                    evidence_text=text[:1200], quality=E.QUALITY_LOW,
                    layer=E.LAYER_OPINION,
                    limitations=[
                        "论坛观点，非事实；样本与代表性需人工判断",
                        f"发布时间 {r.published_at or '未知'}"])
                # 论坛必须保留：帖子URL/发布时间/样本数/正负面观点/局限
                forum_summaries.append({
                    "url": r.url, "published_at": r.published_at,
                    "sample_size": r.extra.get("sample_size") or r.extra.get("replies"),
                    "positive": _sentiment_hits(text, _POS_WORDS),
                    "negative": _sentiment_hits(text, _NEG_WORDS),
                    "limitations": ["单帖观点，样本可能不具代表性"],
                })
            else:
                rec = _rec(
                    entity=r.title, field="web_content", value=None,
                    source_type=source_type, source_url=r.url, source_title=r.title,
                    source_version=r.published_at, collected_at=db.now(),
                    evidence_text=text[:1200],
                    quality=E.QUALITY_HIGH if source_type == E.SRC_OFFICIAL else E.QUALITY_MEDIUM,
                    layer=E.LAYER_FACT,
                    limitations=["网页/官方页快照，时效与权威性以来源为准"])
            records.append(rec.to_dict())
        findings = {"count": len(records), "query": req.query,
                    "searched_domains": domains or []}
        if forum_summaries:
            findings["forum_posts"] = forum_summaries
        # 区分摘要与正文：正文已抓取/仅摘要都如实标记在记录限制里；并对网络正文做字段抽取
        # 字段级抽取（只对非论坛来源；论坛观点不提升为事实）
        extracted = _extract_field_values_index(req, results)
        for hit in extracted:
            records.append(_rec(
                entity=hit["idq_entity"], field=hit["field"], value=hit["value"],
                unit=hit["unit"], source_type=hit["source_type"], source_url=hit["url"],
                source_title=hit["title"], source_version=hit["published_at"],
                evidence_text=hit["text"], quality=E.QUALITY_LOW,
                layer=E.LAYER_UNCONFIRMED,
                limitations=["自网页正文/摘要正则抽取，未人工核验；作参考不作已核验事实"]).to_dict())
        return sa.SubagentResult(subagent=spec.id, ok=True, evidence=records,
                                 findings=findings,
                                 limitations=["按 query 检索结果页并提取正文，非抓首页"])

    # ---------- GEO ----------

    def _run_geo(self, spec, req, tid, rid, scenario) -> sa.SubagentResult:
        center = req.extra.get("center") or req.extra.get("location")
        if not center:
            return sa.SubagentResult(
                subagent=spec.id, ok=False, needs_user=True,
                question="地理分析需要中心地点（center/location）。", error="missing_center")
        radius = float(req.extra.get("radius_km", 2))
        try:
            out = self._call_tool(spec, "geo_radius_search",
                                  {"center": center, "radius_km": radius,
                                   "filters": req.extra.get("filters"),
                                   "sort_by": req.extra.get("sort_by", "distance")},
                                  tid, rid, spec.id, scenario)
        except FileNotFoundError:
            return sa.SubagentResult(subagent=spec.id, ok=False, needs_user=True,
                                     question="地理工具未启用（无 geo 插件）。",
                                     error="geo_tool_unavailable")
        src = (out.get("source") or "api")
        st = E.SRC_MOCK if src in ("local_geojson", "mock") else E.SRC_API
        records = []
        for c in out.get("candidates", []) or []:
            props = c.get("properties", c) or {}
            # 距离/坐标为可核事实；评分/营业/环境为未确认属性
            records.append(_rec(
                entity=props.get("name", c.get("name", "")), field="distance_km",
                value=c.get("distance_km", props.get("distance_km")), unit="km",
                source_type=st, source_title=f"geo:{src}", collected_at=db.now(),
                evidence_text=f"{props.get('category','')} 直线距离 {c.get('distance_km','?')} km",
                quality=E.QUALITY_HIGH if st == E.SRC_API else E.QUALITY_MEDIUM,
                layer=E.LAYER_FACT,
                limitations=["Haversine 直线距离，非道路路线"]).to_dict())
            for attr, label in (("rating", "评分"), ("hours", "营业状态"), ("good_for_work", "环境/安静度")):
                if props.get(attr) is not None:
                    records.append(_rec(
                        entity=props.get("name", ""), field=attr, value=props.get(attr),
                        source_type=st, source_title=f"geo:{src}", layer=E.LAYER_UNCONFIRMED,
                        quality=E.QUALITY_LOW,
                        limitations=[f"{label}为样例/未核验属性，距离通过不代表其真实"]).to_dict())
        return sa.SubagentResult(
            subagent=spec.id, ok=True, evidence=records,
            findings={"count": len(records), "source": src, "crs": out.get("crs")},
            limitations=["无外部路线 provider 时不提供道路通勤时间",
                         "样例数据仅坐标/类别/直线距离可核"])

    # ---------- 纯计算型 ----------

    def _run_normalize(self, req, records: list[dict]) -> sa.SubagentResult:
        normalized, aliases = [], {}
        for r in records:
            rec = E.EvidenceRecord.from_dict(r)
            evid = _stable_evidence_id(r)
            val, unit = normalize_value(rec.value, rec.field, rec.unit)
            canon_entity, ent_aliases = canonicalize_entity(rec.entity)
            for a in ent_aliases:
                aliases.setdefault(a, canon_entity)
            conv = ""
            out_unit = unit or rec.unit
            if rec.value is not None and str(rec.value) != str(val):
                conv = f"{rec.value} {rec.unit or ''} → {val} {out_unit or ''}".strip()
            d = {**rec.to_dict(), "id": evid, "value": val,
                 "unit": out_unit, "entity_canonical": canon_entity,
                 "conversion": conv, "normalized": True}
            if rec.source_version:
                d["version"] = rec.source_version
            normalized.append(d)
        return sa.SubagentResult(
            subagent=sa.SA_NORMALIZER, ok=True, evidence=records,
            normalized=normalized,
            findings={"count": len(normalized), "entity_aliases": aliases,
                      "units": sorted({(n.get("unit") or "") for n in normalized})})

    def _run_verify(self, req, records: list[dict]) -> sa.SubagentResult:
        recs = [E.EvidenceRecord.from_dict(r) for r in records]
        conflicts = E.detect_conflicts(recs)
        groups = E.layerize(recs)
        return sa.SubagentResult(
            subagent=sa.SA_EVIDENCE_VERIFIER, ok=True, evidence=records,
            findings={
                "conflicts": conflicts,
                "layer_counts": {k: len(v) for k, v in groups.items()},
                "unconfirmed": len(groups.get(E.LAYER_UNCONFIRMED, [])),
                "opinions": len(groups.get(E.LAYER_OPINION, [])),
            },
            limitations=["证据核验只判定来源/冲突/层级，不替用户做业务结论"])

    def _run_compare(self, req, records: list[dict]) -> sa.SubagentResult:
        if not req.required_fields:
            return sa.SubagentResult(
                subagent=sa.SA_COMPARISON, ok=False, needs_user=True,
                question="对比需要用户确认的比较标准（required_fields，如 价格/距离/评分）。",
                error="missing_comparison_criteria")
        # 按规范实体聚合 field->多值列表；消费标准化结果，缺失字段不补零，冲突值全保留
        table: dict[str, dict] = {}
        for r in records:
            rec = E.EvidenceRecord.from_dict(r)
            # 纳入事实与"未确认但带来源"两项；观点/推荐/缺失一律不进对照表
            if rec.layer not in (E.LAYER_FACT, E.LAYER_UNCONFIRMED) or rec.field not in req.required_fields:
                continue
            entity = r.get("entity_canonical") or rec.entity
            cell = table.setdefault(entity, {}).setdefault(rec.field, {
                "values": [], "units": [], "evidence_ids": [], "sources": [],
                "conversions": [], "confirmed_flags": []})
            cell["values"].append(rec.value)
            cell["units"].append(rec.unit)
            cell["confirmed_flags"].append(rec.layer == E.LAYER_FACT)
            evid = r.get("id") or ""
            if evid:
                cell["evidence_ids"].append(evid)
            src = r.get("source_url") or r.get("source_title") or r.get("source_type")
            if src:
                cell["sources"].append(src)
            if r.get("conversion"):
                cell["conversions"].append(r["conversion"])
        rows = []
        for entity, fields in sorted(table.items()):
            attrs = {}
            for field, cell in fields.items():
                attrs[field] = {
                    "value": cell["values"][-1],
                    "all_values": cell["values"],
                    "all_units": cell["units"],
                    "unit": cell["units"][-1],
                    "evidence_ids": cell["evidence_ids"],
                    "sources": cell["sources"],
                    "conversions": cell["conversions"],
                    "confirmed": all(bool(f) for f in cell["confirmed_flags"]),
                }
            rows.append({"entity": entity, "attributes": attrs})
        weights = (req.extra or {}).get("weights") or {}
        scored = None
        score_note = None
        if weights:
            scored, score_note = _transparent_scores(table, weights)
        findings = {"comparison": rows, "criteria": req.required_fields}
        if scored is not None:
            findings["weighted_scores"] = scored
            findings["score_note"] = score_note
        return sa.SubagentResult(
            subagent=sa.SA_COMPARISON, ok=True, evidence=records, findings=findings,
            limitations=[
                "对照表只列事实，不替用户做最终选择（推荐/取舍交用户）",
                "同一实体同字段多值保留为 all_values（冲突不覆盖,需人工判断）",
                "评分是用户给定权重的透明线性归一，不是客观事实" if weights else
                "未提供权重，仅给事实对照（不计算推荐分）"])


_POS_WORDS = ["好", "满意", "推荐", "靠谱", "省油", "值得", "不错", "喜欢", "稳定", "省心"]
_NEG_WORDS = ["差", "后悔", "故障", "坑", "投诉", "费油", "异响", "失望", "问题", "烧机油", "割韭菜"]


def _sentiment_hits(text: str, words: list[str]) -> list[str]:
    return [w for w in words if w in (text or "")]


# 单位标准化表：目标单位 + 换算系数
_UNIT_TABLE = {
    # 距离 -> km
    "m": ("km", 0.001), "米": ("km", 0.001),
    "公里": ("km", 1.0), "千米": ("km", 1.0), "km": ("km", 1.0),
    # 容量 -> L（升）
    "ml": ("L", 0.001), "毫升": ("L", 0.001),
    "l": ("L", 1.0), "L": ("L", 1.0), "升": ("L", 1.0),
    # 金额 -> 元
    "k": ("元", 1000.0), "千": ("元", 1000.0), "千块": ("元", 1000.0),
    "万": ("元", 10000.0), "万元": ("元", 10000.0), "块": ("元", 1.0), "元": ("元", 1.0),
    # 时间 -> 分钟
    "小时": ("分钟", 60.0), "h": ("分钟", 60.0),
    "天": ("分钟", 1440.0), "秒": ("分钟", 1 / 60.0),
}
# 能耗特殊：L/100km 与 kWh/100km 不互相换算（能量类型不同），仅规范化大小写
_ENERGY_UNITS = {"l/100km": "L/100km", "升/百公里": "L/100km",
                 "kwh/100km": "kWh/100km", "度/百公里": "kWh/100km"}


def normalize_value(value: Any, field: str | None = None, unit: str | None = None):
    """金额/距离/容量/时间统一单位；能耗仅规范写法（不同能量不混算）。无法标准则原样。"""
    if value is None:
        return value, unit
    u = (unit or "").strip()
    low = u.lower()
    if low in _ENERGY_UNITS:
        try:
            return float(value), _ENERGY_UNITS[low]
        except (TypeError, ValueError):
            return value, _ENERGY_UNITS[low]
    if u in _UNIT_TABLE:
        target, factor = _UNIT_TABLE[u]
        try:
            return round(float(value) * factor, 4), target
        except (TypeError, ValueError):
            return value, unit
    return value, unit


def _standardize(value: Any, unit: str) -> tuple[Any, str]:
    return normalize_value(value, None, unit)


# 实体别名/型号/版本：把常见同义写法归并到规范实体（可被领域资料扩展）
_ENTITY_ALIAS_RULES = [
    # (规范名, [别名/型号包含词])
]


def canonicalize_entity(entity: str | None):
    """返回 (规范实体, 命中别名集合)。通用归并：去全部空白 + 小写化，
    使 "B  X"/"BX"、大小写差异等同一实体对齐；显式别名规则仍可经领域资料扩展。
    型号/版本保留在 evidence 原文，不抹掉。"""
    if not entity:
        return entity, set()
    name = str(entity).strip()
    norm = re.sub(r"\s+", "", name).casefold().rstrip(".")
    hits = set()
    for canon, aliases in _ENTITY_ALIAS_RULES:
        if norm == re.sub(r"\s+", "", str(canon)).casefold():
            return canon, set(aliases)
        for a in aliases:
            if a and re.sub(r"\s+", "", str(a)).casefold() in norm:
                hits.add(name)
                return canon, hits
    return (norm if norm != name else name), set()


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _transparent_scores(table: dict, weights: dict) -> tuple[list[dict], str]:
    """按用户给定权重做透明线性归一评分。

    每个维度 min-max 归一到 0..1；cost/price 等"越小越好"维度用反向归一。
    明确标注：这是用户权重下的排序工具，不是客观事实，也不替用户决策。
    """
    smaller_better = {"price", "cost", "fuel_consumption", "energy_consumption",
                      "distance_km", "fee"}
    cols = [c for c in weights if c]
    col_vals: dict[str, list[float]] = {c: [] for c in cols}
    for attrs in table.values():
        for c in cols:
            cell = attrs.get(c)
            if cell:
                fv = _to_float(cell.get("value"))
                if fv is not None:
                    col_vals[c].append(fv)
    totals = {e: 0.0 for e in table}
    norm_detail = {e: {} for e in table}
    total_w = 0.0
    for c in cols:
        try:
            w = float(weights[c])
        except (TypeError, ValueError):
            continue
        vals = col_vals[c]
        if not vals:
            continue
        lo, hi = min(vals), max(vals)
        total_w += w
        for e, attrs in table.items():
            cell = attrs.get(c)
            fv = _to_float(cell.get("value")) if cell else None
            if fv is None:
                continue
            n = 1.0 if hi == lo else (fv - lo) / (hi - lo)
            if c in smaller_better:
                n = 1.0 - n
            totals[e] += w * n
            norm_detail[e][c] = round(n, 3)
    ranked = sorted(
        ({"entity": e, "score": round(totals[e] / total_w, 3) if total_w else None,
          "dimension_norm": norm_detail[e]} for e in table),
        key=lambda x: (x["score"] is None, -(x["score"] or 0)))
    note = ("评分=用户权重下各维度 min-max 线性归一（成本/能耗/距离反向），"
            "缺失数据不计入；为透明排序参考，非客观事实，最终选择由用户决定。")
    return ranked, note


def _stable_evidence_id(r: dict) -> str:
    """基于来源/实体构建稳定证据 ID（同一条源证据跨标准化可追溯）。"""
    raw = r.get("id") or ""
    if raw:
        return raw
    import hashlib
    seed = "|".join(str(r.get(k) or "") for k in
                    ("source_url", "entity", "field", "value", "collected_at"))
    return "e_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]



# 字段级数字抽取：按任务 required_fields 从正文/摘要里提取 (值,单位,原文)
_FIELD_UNIT_PAT = {
    "price": [("元", r"元|块|¥|￥"), ("元_万", r"万|万块"), ("美元", r"[$]")],
    "cost": [("元", r"元|块|¥|￥"), ("元_万", r"万")],
    "budget": [("元", r"元|块|¥|￥"), ("元_万", r"万")],
    "fee": [("元", r"元|块|¥|￥")],
    "range": [("km", r"km|公里|千米"), ("m", r"米|m\b")],
    "distance": [("km", r"km|公里|千米"), ("m", r"米|m\b")],
    "distance_km": [("km", r"km|公里|千米|公里数")],
}


def _extract_field_values(text: str, required_fields: list[str],
                          entity: str, source_type: str, url: str, title: str, published_at=None,
                          whitespace_norm: bool = True) -> list[dict]:
    """从一段正文抽出 required_fields 的首个数字+单位命中，保留原文与来源。"""
    if not text or not required_fields:
        return []
    text = re.sub(r"\s+", " ", text or "")
    hits = []
    for field in required_fields:
        pats = _FIELD_UNIT_PAT.get(field, [])
        if not pats:
            # 未知字段：宽松抓"数字 + 已知单位"（维度中性）
            pats = [("", r"(?:km|公里|千米|米|元|块|万|L|升|kWh|%|Kg|吨|分钟|小时)")]
        found = False
        for unit, alt in pats:
            mre = re.search(r"(?P<num>\d+(?:[,，.][\d]+)?)\s*(?P<unit>" + alt + r")", text, re.I)
            if not mre:
                continue
            try:
                num = float(mre.group("num").replace(",", "").replace("，", ""))
            except ValueError:
                continue
            start = max(0, mre.start() - 30)
            raw = text[start:mre.end() + 30]
            hits.append({
                "field": field, "value": num, "unit": unit or mre.group("unit"),
                "text": raw[:200], "entity": entity, "source_type": source_type,
                "url": url, "title": title, "published_at": published_at,
            })
            found = True
            break
    return hits


def _extract_field_values_index(req, results) -> list[dict]:
    """按搜索结果批量抽取字段；跳过论坛（观点不提升为事实）。"""
    fields = (req.required_fields or [])
    if not fields:
        return []
    out = []
    _ST_MAP = {"web": E.SRC_API, "official": E.SRC_OFFICIAL, "api": E.SRC_API,
               "document": E.SRC_DOCUMENT}
    for r in results:
        if getattr(r, "source_kind", "") == "forum":
            continue
        text = r.content or r.snippet
        for hit in _extract_field_values(text, fields, r.title, E.SRC_API,
                                         r.url, r.title, r.published_at):
            hit["source_type"] = _ST_MAP.get(r.source_kind, E.SRC_API)
            hit["idq_entity"] = hit.pop("entity")
            out.append(hit)
    return out
