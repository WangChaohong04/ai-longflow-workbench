"""Batch6 追加：分段检索（预算化改写/扩召回 + 阶段记录）与采购硬约束核验。"""
from tests.conftest import workdir  # noqa: F401

import longflow.db as db
import longflow.rag as rag
from longflow import verification as V


def _chunk(cid, doc, text, scenario="team_ops", expires=None):
    return {"chunk_id": cid, "doc_name": doc, "section": "", "text": text}


def test_rewrite_expands_synonyms():
    q = rag.rewrite_query("出差住宿标准")
    assert "酒店" in q or "宾馆" in q  # 扩展同义词
    assert "住宿" in q


def test_search_staged_records_stages_and_budget(workdir):
    conn = db.connect(str(workdir / "s.db"))
    db.init_db(conn)
    rag._ensure_rag_cols(conn)
    # 只放一条用"酒店"措辞的资料；原查询用"住宿"也能命中（含 bigram 近似），
    # 这里验证阶段记录结构与最多轮次预算。
    conn.execute(
        """INSERT INTO knowledge_chunks
           (id, source_id, doc_name, section, text, fields_json, citations_json, scenario)
           VALUES ('kc1','s','hotel.md','x','员工出差酒店住宿每晚限额 500元 凭票报销','{}','[]','team_ops')""")
    conn.commit()
    out = rag.search_staged(conn, "出差住宿标准限额", scenario="team_ops", top_k=5, max_rounds=2)
    assert "stages" in out and out["stages"]
    assert out["stages"][0]["round"] == 1
    assert all("elapsed_ms" in s and "hits" in s for s in out["stages"])
    assert len(out["stages"]) <= 2  # 预算上限


def test_search_staged_stops_when_enough(workdir):
    conn = db.connect(str(workdir / "e.db"))
    db.init_db(conn)
    rag._ensure_rag_cols(conn)
    for i in range(6):
        conn.execute(
            """INSERT INTO knowledge_chunks
               (id, source_id, doc_name, section, text, fields_json, citations_json, scenario)
               VALUES (?,?,?, 'x', ?,'{}','[]','team_ops')""",
            (f"kc{i}", "s", f"d{i}.md", f"差旅住宿标准条款 {i} 限额 500元 报销规定"))
    conn.commit()
    out = rag.search_staged(conn, "差旅住宿标准限额报销", scenario="team_ops", top_k=5, max_rounds=2)
    # 首轮即足够 -> 不应进入第二轮改写
    assert len(out["stages"]) == 1
    assert out["stages"][0]["rewritten"] is False


def test_extract_amount_threshold_and_vendors():
    chunks = [_chunk("c1", "procurement.md",
                     "单次采购金额超过 5000 元（含）须经部门总监审批。标准供应商名录包含：\n"
                     "- approved-vendor（名录内）\n- office-mart（名录内）。"
                     "注意：unreachable-vendor 不在公司标准供应商名录中，向其下单属于违规采购。")]
    assert V.extract_amount_threshold(chunks) == 5000
    vendors = V.extract_approved_vendors(chunks)
    assert "approved-vendor" in vendors


def test_purchase_constraint_flags_unlisted_vendor():
    chunks = [_chunk("c1", "procurement.md",
                     "单次采购金额超过 5000 元须审批。名录包含 approved-vendor；"
                     "unreachable-vendor 不在公司标准供应商名录中，向其下单属于违规采购。")]
    bad = V.check_purchase_constraints(
        {"vendor": "unreachable-vendor", "amount": 1000}, chunks)
    assert bad["ok"] is False
    assert any(p["kind"] == "vendor_not_listed" for p in bad["problems"])


def test_purchase_constraint_flags_amount_threshold():
    chunks = [_chunk("c1", "procurement.md", "单次采购金额超过 5000 元（含）须经部门总监审批。")]
    r = V.check_purchase_constraints({"vendor": "approved-vendor", "amount": 8000}, chunks)
    assert r["needs_approval"] is True
    assert r["amount_threshold"] == 5000


def test_purchase_constraint_unknown_when_no_evidence():
    # 无证据：不编造规则，vendor_ok 未知
    r = V.check_purchase_constraints({"vendor": "whatever-co", "amount": 100}, [])
    assert r["ok"] is True
    assert r["vendor_ok"] is None
    assert r["amount_threshold"] is None
