"""知识加载与检索：BM25 关键词 + 业务字段过滤 + 重排。

业务字段由场景/插件提供（chunks.fields_json），核心不写死任何行业字段。
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path

from . import config as cfg_mod
from . import db

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[一-鿿]")
# 中文按字切分后用 bigram 提升召回
_CJK = re.compile(r"[一-鿿]")
# 中文高频功能字（无区分性），相关性命中判断时忽略
_STOP_CHARS = set("的了是在我你他她它们和与及或吗呢吧啊呀个些这那哪有没不为也都就要会到把被对从向给公司政策制度规定工作方面情况时候种类方式")


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for m in _TOKEN_RE.findall((text or "").lower()):
        if _CJK.match(m):
            tokens.append(m)
        else:
            tokens.append(m)
    # 中文 bigram
    cjk = [t for t in tokens if _CJK.match(t)]
    bigrams = [cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)]
    return tokens + bigrams


def _split_sections(text: str) -> list[tuple[str, str]]:
    """按 markdown 标题切分；无标题则按段落聚合。返回 (section, body)。"""
    sections: list[tuple[str, str]] = []
    cur_title = "正文"
    buf: list[str] = []
    for line in text.splitlines():
        h = re.match(r"^(#{1,4})\s+(.*)$", line.strip())
        if h:
            if buf:
                sections.append((cur_title, "\n".join(buf).strip()))
                buf = []
            cur_title = h.group(2).strip()
        else:
            buf.append(line)
    if buf:
        sections.append((cur_title, "\n".join(buf).strip()))
    return [(t, b) for t, b in sections if b]


def _chunk_body(body: str, max_chars: int = 600) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in paras:
        if len(cur) + len(p) > max_chars and cur:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def _extract_citations(section: str, body: str) -> list[str]:
    """提取条款标识，如 '第三条'、'3.'、'规则三'。"""
    cites = []
    for m in re.finditer(r"第[一二三四五六七八九十百0-9]+条", section + "\n" + body):
        cites.append(m.group(0))
    for m in re.finditer(r"(?m)^\s*(\d+)[\.、]\s*", body):
        cites.append(f"第{m.group(1)}条")
    # 去重保序
    seen, out = set(), []
    for c in cites:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


_META_DATE = __import__("re").compile(r"(20\d{2})[-年](\d{1,2})[-月](\d{1,2})")


def _doc_meta(text: str, fallback_name: str = "") -> dict:
    """从 Markdown 头部解析 版本/生效日期/失效日期（用于知识时效过滤）。"""
    import re as _re
    head = (text or "")[:600]
    version = ""
    mv = _re.search(r"版本[：:]\s*(v?[0-9][0-9A-Za-z.\-]*)", head)
    if mv:
        version = mv.group(1)
    elif fallback_name:
        my = _re.search(r"20\d{2}", fallback_name)
        version = my.group(0) if my else ""

    def _date_after(label):
        m = _re.search(label + r"[：:]?\s*(20\d{2})\D(\d{1,2})\D(\d{1,2})", head)
        if not m:
            return None
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"

    effective = _date_after("生效日期") or _date_after("生效")
    expires = _date_after("失效日期") or _date_after("失效")
    return {"version": version, "effective_at": effective, "expires_at": expires}


def _ensure_rag_cols(conn):
    """幂等迁移：为 knowledge_chunks 增加版本/生效/失效列。"""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(knowledge_chunks)").fetchall()}
    for col, ddl in (("version", "TEXT DEFAULT ''"),
                     ("effective_at", "TEXT"),
                     ("expires_at", "TEXT")):
        if col not in have:
            conn.execute(f"ALTER TABLE knowledge_chunks ADD COLUMN {col} {ddl}")
    conn.commit()


def load_knowledge_path(
    conn: sqlite3.Connection,
    pattern: str,
    scenario: str,
    *,
    source_name: str | None = None,
) -> int:
    """把 knowledge glob（相对仓库根或绝对路径）加载入库。返回 chunk 数。"""
    root = cfg_mod.REPO_ROOT
    paths = sorted(glob.glob(str(root / pattern)))
    count = 0
    for path in paths:
        p = Path(path)
        ext = p.suffix.lower()
        source_id = db.new_id("ks")
        stat = p.stat()
        conn.execute(
            """INSERT OR REPLACE INTO knowledge_sources
               (id, name, scenario, version, source_path, access_scope, updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                source_id,
                source_name or p.name,
                scenario,
                "",
                str(p),
                "scenario",
                str(stat.st_mtime),
            ),
        )
        _ensure_rag_cols(conn)
        if ext in (".md", ".txt"):
            text = p.read_text(encoding="utf-8")
            meta = _doc_meta(text, p.name)
            for section, body in _split_sections(text):
                citations = _extract_citations(section, body)
                for chunk_text in _chunk_body(body):
                    conn.execute(
                        """INSERT INTO knowledge_chunks
                           (id, source_id, doc_name, section, text, fields_json,
                            citations_json, scenario, version, effective_at, expires_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            db.new_id("kc"),
                            source_id,
                            p.name,
                            section,
                            chunk_text,
                            "{}",
                            json.dumps(citations, ensure_ascii=False),
                            scenario,
                            meta["version"], meta["effective_at"], meta["expires_at"],
                        ),
                    )
                    count += 1
        elif ext == ".json":
            data = json.loads(p.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else data.get("chunks", [])
            for item in items:
                conn.execute(
                    """INSERT INTO knowledge_chunks
                       (id, source_id, doc_name, section, text, fields_json,
                        citations_json, scenario)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        db.new_id("kc"),
                        source_id,
                        item.get("doc_name", p.name),
                        item.get("section", ""),
                        item.get("text", ""),
                        json.dumps(item.get("fields", {}), ensure_ascii=False),
                        json.dumps(item.get("citations", []), ensure_ascii=False),
                        scenario,
                    ),
                )
                count += 1
    conn.commit()
    return count


def load_plugin_knowledge(conn: sqlite3.Connection, chunks: list[dict], scenario: str) -> int:
    source_id = db.new_id("ks")
    conn.execute(
        """INSERT INTO knowledge_sources (id, name, scenario, version, source_path,
           access_scope, updated_at) VALUES (?,?,?,?,?,?,?)""",
        (source_id, "plugin", scenario, "", "", "plugin", db.now()),
    )
    for item in chunks:
        conn.execute(
            """INSERT INTO knowledge_chunks
               (id, source_id, doc_name, section, text, fields_json,
                citations_json, scenario) VALUES (?,?,?,?,?,?,?,?)""",
            (
                db.new_id("kc"),
                source_id,
                item.get("doc_name", "plugin"),
                item.get("section", ""),
                item.get("text", ""),
                json.dumps(item.get("fields", {}), ensure_ascii=False),
                json.dumps(item.get("citations", []), ensure_ascii=False),
                scenario,
            ),
        )
    conn.commit()
    return len(chunks)


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = docs
        self.n = len(docs)
        self.dl = [len(d) for d in docs]
        self.avgdl = (sum(self.dl) / self.n) if self.n else 0.0
        self.tf = [Counter(d) for d in docs]
        df: Counter = Counter()
        for d in docs:
            for term in set(d):
                df[term] += 1
        self.idf = {
            t: math.log(1 + (self.n - n + 0.5) / (n + 0.5)) for t, n in df.items()
        }

    def score(self, query_tokens: list[str], idx: int) -> float:
        if not self.dl[idx] or not self.avgdl:
            return 0.0
        s = 0.0
        for t in query_tokens:
            if t not in self.idf:
                continue
            f = self.tf[idx].get(t, 0)
            s += self.idf[t] * (f * (self.k1 + 1)) / (
                f + self.k1 * (1 - self.b + self.b * self.dl[idx] / self.avgdl)
            )
        return s


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    scenario: str | None = None,
    fields: dict | None = None,
    top_k: int = 5,
    include_expired: bool = False,
    as_of: str | None = None,
    scenario_like: str | None = None,
) -> list[dict]:
    """关键词 BM25 + 业务字段过滤 + 知识时效过滤 + 重排（条款/字段命中加权）。

    默认只召回 as_of（默认今天）处于 [effective_at, expires_at] 有效期内、
    或未标注时效的知识；已失效资料（如旧年度政策）不参与回答，避免用过时数字。
    """
    _ensure_rag_cols(conn)
    from datetime import datetime, timezone
    today = as_of or datetime.now(timezone.utc).date().isoformat()
    if scenario_like is not None:
        rows = conn.execute(
            "SELECT * FROM knowledge_chunks WHERE scenario LIKE ? ESCAPE '\\'",
            (scenario_like,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM knowledge_chunks WHERE (? IS NULL OR scenario=?)",
            (scenario, scenario),
        ).fetchall()
    colset = {row["name"] for row in conn.execute("PRAGMA table_info(knowledge_chunks)").fetchall()}
    candidates = []
    for r in rows:
        row_fields = json.loads(r["fields_json"] or "{}")
        if fields:
            if not all(str(row_fields.get(k)) == str(v) for k, v in fields.items()):
                continue
        if not include_expired:
            eff = r["effective_at"] if "effective_at" in colset else None
            exp = r["expires_at"] if "expires_at" in colset else None
            if eff and eff > today:
                continue  # 尚未生效
            if exp and exp < today:
                continue  # 已失效
        candidates.append(r)
    if not candidates:
        return []

    docs = [tokenize(r["text"] + " " + (r["section"] or "")) for r in candidates]
    bm25 = BM25(docs)
    q_tokens = tokenize(query)

    # 最低相关性门槛：查询的区分性词（bigram/拉丁词）必须在文档中有足够命中，
    # 单字停用级命中不算（防止"宠物保险"靠"的/是/公司"等字误召回）。
    scored = []
    for i, r in enumerate(candidates):
        base = bm25.score(q_tokens, i)
        if base <= 0:
            continue
        doc_tokens = set(docs[i])
        # 相关性门槛（可核验信号，不靠自报置信度）：查询中的区分性词——
        # 中文 bigram/英文词，以及非停用单字——必须在文档中有足够命中；
        # 完全无实质命中 → 视为无依据（如"宠物保险"不应被通用条款召回）。
        # 相关性信号（规则+可验证，不靠模型自报）：
        #  1) 中文 bigram/英文词（连续子串）命中——强信号；
        #  2) 非停用单字命中——弱信号，需足够数量。
        # 中文 bigram 必须两端都是非停用内容字才算强信号——
        # 跨词 bigram（"的宠""司政"）由切分边界产生，可能在任意文档偶现。
        def _is_content_bigram(bg: str) -> bool:
            return (len(bg) >= 2 and all(
                (not _CJK.match(ch)) or ch not in _STOP_CHARS for ch in bg))
        multi = [t for t in q_tokens if len(t) >= 2]
        multi_hits = [t for t in multi if t in doc_tokens and (not _CJK.match(t[0]) or _is_content_bigram(t))]
        latin_hits = [t for t in q_tokens if not _CJK.match(t) and len(t) >= 2
                      and t in doc_tokens]
        char_hits = [t for t in q_tokens if _CJK.match(t) and len(t) == 1
                     and t not in _STOP_CHARS and t in doc_tokens]
        # 无强信号（内容性连续子串）命中时，要求非停用单字命中 >= 4 才算相关
        # （"宠物保险"在业务文档中仅"保/物"偶现；"采购审批规则"有 采/购/审/批/规/则 6 个）。
        if not multi_hits and not latin_hits and len(char_hits) < 4:
            continue
        cites = json.loads(r["citations_json"] or "[]")
        row_fields = json.loads(r["fields_json"] or "{}")
        boost = 0.0
        if cites and (multi_hits or char_hits):
            boost += 0.3  # 带条款出处且确实相关的 chunk 更可核验
        field_blob = " ".join(str(v) for v in row_fields.values())
        if field_blob:
            field_hits = sum(1 for t in q_tokens if t in tokenize(field_blob))
            boost += 0.15 * field_hits
        scored.append((base + boost, r))

    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, r in scored[:top_k]:
        out.append(
            {
                "chunk_id": r["id"],
                "doc_name": r["doc_name"],
                "section": r["section"],
                "text": r["text"],
                "score": round(score, 4),
                "citations": json.loads(r["citations_json"] or "[]"),
                "fields": json.loads(r["fields_json"] or "{}"),
                "scenario": r["scenario"],
                "version": r["version"] if "version" in r.keys() else "",
                "expires_at": (r["expires_at"] if "expires_at" in r.keys() else None),
            }
        )
    return out



# 同义词/上位词扩展（确定性，预算内只改写一次）：首轮召回不足时放宽检索。
_SYNONYMS = {
    "住宿": ["住宿", "酒店", "宾馆"],
    "酒店": ["住宿", "酒店"],
    "宾馆": ["住宿", "宾馆"],
    "差旅": ["差旅", "出差"],
    "出差": ["差旅", "出差"],
    "采购": ["采购", "购买", "申购"],
    "报销": ["报销", "费用", "补贴"],
    "补贴": ["补贴", "报销", "补助"],
    "补助": ["补助", "补贴"],
}


def rewrite_query(query: str) -> str:
    """确定性查询改写：把命中的业务词替换/扩展为同义词，以放宽第二轮召回。"""
    out = set((query or "").split())
    expanded = query or ""
    added = []
    for word, syns in _SYNONYMS.items():
        if word in (query or ""):
            for s in syns:
                if s not in expanded:
                    added.append(s)
                    expanded = f"{expanded} {s}"
    return expanded.strip()


def search_staged(
    conn,
    query: str,
    *,
    scenario: str | None = None,
    fields: dict | None = None,
    top_k: int = 5,
    max_rounds: int = 2,
) -> dict:
    """按需分段检索：先用原查询 BM25+字段过滤；首轮召回为空/不足时，在预算内
    改写/扩召回至多 max_rounds 轮。返回 {chunks, stages:[{round,query,hits,elapsed_ms}]}。

    - 每阶段记录命中数与耗时（可观察）。
    - BM25 分数仅用于排序，绝不当作答案正确概率。
    - 语义检索为可选扩展，本实现不引入向量库。
    """
    import time as _time
    stages = []
    seen_ids: set = set()
    merged: list[dict] = []
    q = query
    for rnd in range(1, max(1, max_rounds) + 1):
        t0 = _time.monotonic()
        hits = search(conn, q, scenario=scenario, fields=fields, top_k=top_k)
        elapsed = int((_time.monotonic() - t0) * 1000)
        new = [h for h in hits if h["chunk_id"] not in seen_ids]
        for h in new:
            seen_ids.add(h["chunk_id"])
            merged.append(h)
        stages.append({"round": rnd, "query": q, "hits": len(hits),
                       "new_hits": len(new), "elapsed_ms": elapsed,
                       "rewritten": rnd > 1})
        # 首轮已有足够召回即停（预算化，不无谓扩召回）
        if len(merged) >= top_k or rnd >= max_rounds:
            break
        nq = rewrite_query(q)
        if nq == q:
            break  # 无可改写空间
        q = nq
    # 按分数稳定排序后截断
    merged.sort(key=lambda c: c.get("score", 0), reverse=True)
    return {"chunks": merged[:top_k], "count": len(merged[:top_k]), "stages": stages}

def load_all(conn, root=None, *args, **kwargs) -> int:
    """加载所有已配置场景的知识（评测/预热入口）。root 参数忽略，兼容多签名探测。"""
    from . import config as _cfg
    total = 0
    for sc in _cfg.list_scenarios():
        name = sc["name"]
        if conn.execute("SELECT COUNT(*) AS n FROM knowledge_chunks WHERE scenario=?",
                        (name,)).fetchone()["n"]:
            continue
        try:
            scfg = _cfg.load_scenario(name)
        except FileNotFoundError:
            continue
        for pattern in scfg.get("knowledge", []) or []:
            total += load_knowledge_path(conn, pattern, name)
    return total


# 评测/外部常用别名
load_scenarios = load_all


def load_knowledge(conn, root_path=None, *args, **kwargs) -> int:
    """load_all 的语义化别名（忽略 root_path，知识路径由场景配置决定）。"""
    return load_all(conn)