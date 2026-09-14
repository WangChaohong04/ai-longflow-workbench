"""能力状态总览（脱敏、无密钥）与 调研导出（Markdown/CSV，保留引用/时间/未确认/失败分支）。"""
from __future__ import annotations

import csv
import importlib.util as _u
import io
from datetime import datetime, timezone

from . import db as _db
from .evidence import LAYER_LABELS, LAYER_FACT


def capability_overview(state) -> dict:
    """返回各能力 enabled/ready/说明。仅返回布尔与文本，绝不返回端点/密钥。"""
    cfg = state.cfg
    http = getattr(getattr(state, "runtime", None), "http", None)
    search_endpoint = bool(cfg.get("search_endpoint"))
    search_ready = search_endpoint and bool(cfg.get("allowed_domains")) and http is not None
    llm = cfg.get("llm") or {}
    driver = llm.get("driver", "local")
    llm_real = driver == "openai_compatible" and all(llm.get(k) for k in ("api_key", "base_url", "model"))
    mp = cfg.get("map_plugins") or {}
    active_map = mp.get("active", "amap")
    map_cfg = mp.get(active_map, {}) or {}
    map_enabled = bool(map_cfg.get("enabled", True))
    geo_ready = state.runtime.registry.get("geo_radius_search") is not None
    from . import subagents as sa
    ids = {s.id for s in sa.all_specs()}
    docx_ready = True  # 标准库 ZIP/XML 解析可用
    pdf_ready = bool(_u.find_spec("pypdf") or _u.find_spec("PyPDF2"))
    caps = [
        {"key": "rag", "label": "本地知识检索 (RAG)",
         "enabled": True, "ready": True,
         "desc": "导入且激活的文档/文本分段检索，按 工作区/领域 隔离"},
        {"key": "web_search", "label": "网页检索",
         "enabled": "web_researcher" in ids, "ready": search_ready,
         "desc": "需配置 search_endpoint（含域名白名单/SSRF 校验）；未配置时明确返回『未配置』"},
        {"key": "official", "label": "官方/权威来源",
         "enabled": "official_source_researcher" in ids, "ready": search_ready,
         "desc": "复用网页检索能力；无配置或未召回时返回空/未确认，不伪装已确认"},
        {"key": "forum", "label": "论坛观点",
         "enabled": "forum_researcher" in ids, "ready": search_ready,
         "desc": "仅作为『观点』呈现，不会被提升为已确认事实"},
        {"key": "geo", "label": "地理/空间检索 (GEO)",
         "enabled": "geo_researcher" in ids, "ready": geo_ready,
         "desc": "本地 GeoJSON 与直线距离计算；地图底图、真实地点检索和道路路线须分别配置与验收"},
        {"key": "docx", "label": "DOCX 解析",
         "enabled": True, "ready": docx_ready,
         "desc": "python-docx 优先，缺省时 zip 兜底；支持段落与表格"},
        {"key": "pdf", "label": "PDF 文本解析",
         "enabled": True, "ready": pdf_ready,
         "desc": "需 pypdf；按页分段并标注页码；扫描件页会明确提示"},
        {"key": "ocr", "label": "OCR (扫描件)",
         "enabled": False, "ready": False,
         "desc": "未启用：扫描型 PDF 会提示需 OCR，不会静默激活空内容"},
        {"key": "llm", "label": "模型驱动",
         "enabled": True, "ready": driver in ("local", "base") or llm_real,
         "desc": f"driver={driver}；外部模型配置={'齐全（尚不能证明连通）' if llm_real else '未就绪'}；领域规划使用本地规则"},
    ]
    return {"capabilities": caps, "map_active": active_map,
            "planning_source": "local_rules", "real_services_verified": False}


def gather_evidence(conn, root_id: str) -> list[dict]:
    """收集 coordinator 各研究子节点的证据（含分支/子代理标注）。"""
    rows = []
    for c in _db.list_children(conn, root_id):
        cp = c.plan or {}
        if cp.get("engine") != "coordinator":
            continue
        r = c.result or {}
        br = cp.get("branch")
        for rec in (r.get("evidence") or []):
            row = dict(rec)
            row["_branch"] = br
            row["_subagent"] = cp.get("subagent")
            rows.append(row)
    return rows


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def export_markdown(root, rows: list[dict], failures: list[dict]) -> str:
    res = (root.result or {})
    line = []
    line.append("# 调研导出")
    line.append("")
    line.append(f"- 任务：{root.objective or root.title}")
    line.append(f"- 状态：{root.status}（{res.get('status_bucket', '')}）")
    line.append(f"- 已确认：{'是' if res.get('verified') else '否'}")
    line.append(f"- 成功分支：{', '.join(res.get('branches_ok') or []) or '无'}")
    fb = res.get('branches_failed') or []
    if fb:
        line.append(f"- 失败分支：{', '.join(fb)}")
        for f in failures:
            line.append(f"    - {f.get('branch') or f.get('subagent')}: {f.get('error')}")
    line.append(f"- 证据数：{len(rows)}")
    line.append(f"- 导出时间：{_now()}")
    line.append("")
    line.append("## 证据明细")
    line.append("")
    if not rows:
        line.append("（无证据）")
        line.append("")
    for ev in sorted(rows, key=lambda x: (x.get("_branch") or "", x.get("entity") or "")):
        tag = "（未确认）" if ev.get("layer") != LAYER_FACT else ""
        line.append(f"### [{ev.get('_branch') or 'main'}] {ev.get('entity') or ''}."
                    f"{ev.get('field') or ''} = {ev.get('value')}{ev.get('unit') or ''} {tag}")
        line.append(f"- 层级：{LAYER_LABELS.get(ev.get('layer'), ev.get('layer'))}"
                    f" | 质量：{ev.get('quality')} | 来源：{ev.get('source_type')}")
        src = ev.get("source_title") or ""
        url = ev.get("source_url") or ""
        line.append(f"- 来源：{src} {url}")
        cits = []
        if ev.get("id"):
            cits.append(f"证据ID {ev.get('id')}")
        if ev.get("collected_at"):
            cits.append(f"采集 {ev.get('collected_at')}")
        if ev.get("page") not in (None, ""):
            cits.append(f"页码 {ev.get('page')}")
        if cits:
            line.append(f"- 引用：{' · '.join(cits)}")
        if ev.get("evidence_text"):
            line.append(f"- 原文：{ev.get('evidence_text')}")
        if ev.get("conversion"):
            line.append(f"- 转换：{ev.get('conversion')}")
        line.append("")
    return "\n".join(line)


def export_csv(root, rows: list[dict]) -> str:
    import builtins
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["task", "branch", "subagent", "layer", "confirmed", "entity",
                "field", "value", "unit", "quality", "source_type", "source_title",
                "source_url", "evidence_id", "page", "collected_at", "conversion"])
    ttask = root.objective or root.title
    for ev in rows:
        w.writerow([
            ttask, ev.get("_branch") or "", ev.get("_subagent") or "",
            LAYER_LABELS.get(ev.get("layer"), ev.get("layer")),
            "yes" if ev.get("layer") == LAYER_FACT else "no",
            ev.get("entity") or "", ev.get("field") or "", builtins.str(ev.get("value")) if ev.get("value") is not None else "",
            ev.get("unit") or "", ev.get("quality") or "", ev.get("source_type") or "",
            ev.get("source_title") or "", ev.get("source_url") or "",
            ev.get("id") or "", ev.get("page") or "", ev.get("collected_at") or "",
            ev.get("conversion") or "",
        ])
    return buf.getvalue()
