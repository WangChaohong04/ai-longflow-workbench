"""RAG 文件导入：用户直接输入文字 / 上传 txt、md、docx、pdf。

流程：解析 → 分段 → 元数据标注（来源/页码/标题/版本/时间/权限）→ **检索预览** →
用户确认后**激活**入库；同名旧版本自动归档。按用户/工作区/领域隔离。

边界：
- RAG 只检索"已导入并激活"的知识；未激活的预览文档不参与检索；
- 实时网页/论坛/价格/库存/外部系统数据必须走工具/插件，不经本导入；
- 未经审核的用户反馈不得通过本路径写入正式知识库。
"""
from __future__ import annotations

import dataclasses
import json
import re
from typing import Any

from . import db

SUPPORTED_EXTS = {".txt", ".md", ".docx", ".pdf", ".json"}
STATUS_PREVIEW = "preview"      # 已解析待确认（不参与检索）
STATUS_ACTIVE = "active"        # 用户确认后激活（参与检索）
STATUS_ARCHIVED = "archived"    # 同名旧版本归档

_CHUNK = 600


@dataclasses.dataclass
class ParsedDoc:
    filename: str
    title: str
    version: str
    sections: list[dict]           # [{section, text, page}]
    char_count: int
    source_type: str = "document"
    warnings: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def parse_text(filename: str, raw: str | bytes, *, version: str = "") -> ParsedDoc:
    """解析 txt/md/json 为分段文档；docx/pdf 走专用解析器。"""
    ext = _ext(filename)
    if ext in (".txt", ".md"):
        text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else raw
        return _parse_markdown_like(filename, text, version)
    if ext == ".docx":
        return parse_docx(filename, raw, version=version)
    if ext == ".pdf":
        return parse_pdf(filename, raw, version=version)
    if ext == ".json":
        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        items = data if isinstance(data, list) else data.get("chunks", [])
        sections = [{"section": it.get("section", ""), "text": it.get("text", ""),
                     "page": it.get("page")} for it in items]
        return ParsedDoc(filename, data.get("title", filename) if isinstance(data, dict) else filename,
                         version or "", sections, sum(len(s["text"]) for s in sections))
    raise ValueError(f"不支持的文件类型: {ext}（支持 {sorted(SUPPORTED_EXTS)}）")


def _parse_markdown_like(filename: str, text: str, version: str) -> ParsedDoc:
    title = filename
    m = re.search(r"^#\s+(.+)$", text, re.M)
    if m:
        title = m.group(1).strip()
    vm = re.search(r"版本[:：]?\s*([0-9vV][0-9.\-]*)", text)
    version = version or (vm.group(1) if vm else "")
    sections = []
    # 按 markdown 标题分段，保留页码占位（md 无页码）
    parts = re.split(r"\n(?=#{1,4}\s)", text)
    for part in parts:
        hm = re.match(r"#{1,4}\s+(.+)", part.strip())
        sec = hm.group(1).strip() if hm else (title or "")
        body = re.sub(r"^#{1,4}\s+.*\n?", "", part).strip()
        if body:
            sections.append({"section": sec, "text": body, "page": None})
    if not sections:
        sections = [{"section": title, "text": text.strip(), "page": None}]
    return ParsedDoc(filename, title, version, sections, len(text))


def parse_docx(filename: str, raw: bytes, *, version: str = "") -> ParsedDoc:
    """解析 .docx；优先 python-docx，缺失时尝试 zip+xml 兜底，均不可用则明确报错。"""
    try:
        import docx  # type: ignore
        import io as _io
        doc = docx.Document(_io.BytesIO(raw))
        sections, cur_title, buf = [], filename, []

        def flush():
            if buf and "".join(buf).strip():
                sections.append({"section": cur_title, "text": "\n".join(buf).strip(), "page": None})
        for p in doc.paragraphs:
            style = (p.style.name or "") if p.style else ""
            if style.startswith("Heading") and p.text.strip():
                flush(); buf.clear(); cur_title = p.text.strip()
            elif p.text.strip():
                buf.append(p.text)
        flush()
        # 表格：把每个表格转为单独 section，避免表格内容被忽略
        for ti, table in enumerate(doc.tables, start=1):
            rows = []
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                sections.append({"section": f"表格{ti}", "text": "\n".join(rows).strip(),
                                 "page": None})
        warnings = []
        if not sections:
            warnings.append("文档未解析到任何段落/表格文本：激活将不会写入任何知识")
        return ParsedDoc(filename, filename, version, sections,
                         sum(len(s["text"]) for s in sections), warnings=warnings)
    except ImportError:
        pass
    # zip/xml 兜底（提取 w:t 文本）
    try:
        import io as _io2
        import zipfile
        zf = zipfile.ZipFile(_io2.BytesIO(raw))
        xml = zf.read("word/document.xml").decode("utf-8", errors="ignore")
        texts = re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml)
        body = "".join(texts)
        return ParsedDoc(filename, filename, version,
                         [{"section": filename, "text": body, "page": None}], len(body))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(
            "解析 docx 需要 python-docx（pip install python-docx），且兜底解析失败") from exc


def parse_pdf(filename: str, raw: bytes, *, version: str = "") -> ParsedDoc:
    """解析 .pdf（按页分段，页码是关键元数据）；无 pdf 库时明确报错而非静默。"""
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError:
            raise ValueError(
                "解析 pdf 需要 pypdf（pip install pypdf）；未安装时请先转成 txt/md 再导入")
    import io as _io
    reader = PdfReader(_io.BytesIO(raw))
    sections = []
    blank_pages = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            txt = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            txt = ""
        if txt.strip():
            sections.append({"section": f"第 {i} 页", "text": txt.strip(), "page": i})
        else:
            blank_pages.append(i)
    warnings = []
    if blank_pages:
        warnings.append(
            f"第 {', '.join(map(str, blank_pages[:10]))} 页未提取到文本：很可能是扫描件，"
            "需 OCR 后才能检索；不会静默激活空内容")
    if not sections:
        warnings.append("整份 PDF 未提取到文本（疑似扫描件/图片型 PDF），激活将失败，请先 OCR")
    return ParsedDoc(filename, filename, version, sections,
                     sum(len(s["text"]) for s in sections), warnings=warnings)


# ---------- 预览 / 激活 / 归档 ----------

def preview(conn, *, workspace: str, owner: str, domain: str, doc: ParsedDoc,
            access_scope: str = "workspace") -> str:
    """登记为 preview（不进 knowledge_chunks，不参与检索）。返回 doc_id。"""
    doc_id = db.new_id("doc")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS imported_docs (
             id TEXT PRIMARY KEY, workspace TEXT, owner TEXT, domain TEXT,
             filename TEXT, title TEXT, version TEXT, status TEXT, access_scope TEXT,
             sections_json TEXT, warnings_json TEXT, created_at TEXT, activated_at TEXT)""",
    )
    # 兼容旧库：补 warnings_json 列
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(imported_docs)").fetchall()}
    if "warnings_json" not in cols:
        conn.execute("ALTER TABLE imported_docs ADD COLUMN warnings_json TEXT NOT NULL DEFAULT '[]'")
    conn.execute(
        """INSERT INTO imported_docs
           (id, workspace, owner, domain, filename, title, version, status, access_scope,
            sections_json, warnings_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (doc_id, workspace, owner, domain, doc.filename, doc.title, doc.version,
         STATUS_PREVIEW, access_scope,
         json.dumps(doc.sections, ensure_ascii=False),
         json.dumps(doc.warnings or [], ensure_ascii=False), db.now()),
    )
    conn.commit()
    return doc_id


def preview_summary(conn, doc_id: str, *, full: bool = False) -> dict:
    row = conn.execute("SELECT * FROM imported_docs WHERE id=?", (doc_id,)).fetchone()
    if row is None:
        raise FileNotFoundError(doc_id)
    sections = json.loads(row["sections_json"] or "[]")
    warnings = json.loads(row["warnings_json"] or "[]")
    if full:
        preview = [{"section": s.get("section"), "page": s.get("page"),
                    "text": s.get("text", "")} for s in sections]
    else:
        preview = [{"section": s.get("section"), "page": s.get("page"),
                    "text": s.get("text", "")[:160]} for s in sections[:5]]
    return {
        "doc_id": doc_id, "filename": row["filename"], "title": row["title"],
        "version": row["version"], "status": row["status"], "domain": row["domain"],
        "section_count": len(sections),
        "char_count": sum(len(s.get("text", "")) for s in sections),
        "warnings": warnings,
        "preview": preview,
    }


def preview_trial_query(conn, doc_id: str, query: str, *, top_k: int = 3) -> dict:
    """在预-编文档内做关键词试问（不激活、不改知识库），返回命中分段与引用。"""
    row = conn.execute("SELECT * FROM imported_docs WHERE id=?", (doc_id,)).fetchone()
    if row is None:
        raise FileNotFoundError(doc_id)
    sections = json.loads(row["sections_json"] or "[]")
    terms = _trial_terms(query)
    scored = []
    for s in sections:
        text = s.get("text", "") or ""
        tl = text.lower()
        n = sum(text.lower().count(t) for t in terms) if terms else 0
        if n > 0:
            scored.append({"score": n, "section": s.get("section"),
                           "page": s.get("page"), "text": text})
    scored.sort(key=lambda x: x["score"], reverse=True)
    hits = []
    for h in scored[:top_k]:
        ex = _excerpt(h["text"], terms, 120)
        hits.append({
            "section": h["section"], "page": h["page"], "score": h["score"],
            "excerpt": ex,
            "citation": {
                "doc": row["title"], "filename": row["filename"],
                "version": row["version"], "section": h["section"],
                "page": h.get("page"), "scope": row["access_scope"],
            },
        })
    return {"doc_id": doc_id, "query": query, "hits": hits, "hit_count": len(hits)}


def _trial_terms(query: str) -> list[str]:
    terms = [t.lower() for t in re.findall(r"[\w\u4e00-\u9fff]+", query or "")]
    return [t for t in terms if len(t) >= 2][:12]


def _excerpt(text: str, terms: list[str], width: int = 120) -> str:
    tl = text.lower()
    idx = 0
    for t in terms:
        i = tl.find(t)
        if i >= 0:
            idx = i
            break
    start = max(0, idx - width // 3)
    return text[start:start + width].strip()


def activate(conn, doc_id: str, *, by: str) -> int:
    """用户确认后激活：写入 knowledge_chunks（带 workspace/domain 隔离标记），
    同名（同 workspace+domain+title）旧版本归档。返回写入 chunk 数。"""
    row = conn.execute("SELECT * FROM imported_docs WHERE id=?", (doc_id,)).fetchone()
    if row is None:
        raise FileNotFoundError(doc_id)
    if row["status"] == STATUS_ACTIVE:
        return 0
    sections = json.loads(row["sections_json"] or "[]")
    scenario = f"user:{row['workspace']}:{row['domain']}"  # 隔离命名空间
    # 归档同名旧版
    conn.execute(
        """UPDATE imported_docs SET status=? WHERE workspace=? AND domain=? AND title=?
           AND id<>? AND status=?""",
        (STATUS_ARCHIVED, row["workspace"], row["domain"], row["title"], doc_id, STATUS_ACTIVE))
    source_id = db.new_id("ks")
    conn.execute(
        """INSERT OR REPLACE INTO knowledge_sources
           (id, name, scenario, version, source_path, access_scope, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (source_id, row["title"], scenario, row["version"],
         f"import:{row['filename']}", row["access_scope"], db.now()))
    n = 0
    for sec in sections:
        for chunk_text in _chunk(sec.get("text", "")):
            conn.execute(
                """INSERT INTO knowledge_chunks
                   (id, source_id, doc_name, section, text, fields_json, citations_json,
                    scenario, version)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (db.new_id("kc"), source_id, row["title"],
                 f"{sec.get('section','')}" + (f" p.{sec['page']}" if sec.get("page") else ""),
                 chunk_text,
                 json.dumps({"workspace": row["workspace"], "owner": row["owner"],
                             "page": sec.get("page"), "imported": True}, ensure_ascii=False),
                 "[]", scenario, row["version"]))
            n += 1
    conn.execute("UPDATE imported_docs SET status=?, activated_at=? WHERE id=?",
                 (STATUS_ACTIVE, db.now(), doc_id))
    conn.commit()
    return n


def list_docs(conn, *, workspace: str | None = None, domain: str | None = None,
              status: str | None = None) -> list[dict]:
    q = "SELECT * FROM imported_docs WHERE 1=1"
    args: list[Any] = []
    if workspace:
        q += " AND workspace=?"; args.append(workspace)
    if domain:
        q += " AND domain=?"; args.append(domain)
    if status:
        q += " AND status=?"; args.append(status)
    q += " ORDER BY id"
    rows = conn.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["sections"] = json.loads(d.pop("sections_json") or "[]")
        out.append(d)
    return out


def _chunk(text: str, max_chars: int = _CHUNK) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    out, buf = [], ""
    for para in re.split(r"\n+", text):
        if len(buf) + len(para) > max_chars and buf:
            out.append(buf.strip()); buf = ""
        buf += para + "\n"
    if buf.strip():
        out.append(buf.strip())
    return out


def _ext(filename: str) -> str:
    import os
    return os.path.splitext(filename)[1].lower()
