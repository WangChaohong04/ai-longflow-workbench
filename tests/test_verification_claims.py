"""Batch2：事实-证据核验四档 + 反例（空引用/错误金额/条件相反/夹带无据）。"""
from longflow import verification as V


def _chunk(cid, text):
    return {"chunk_id": cid, "doc_name": "差旅制度", "section": "住宿", "text": text}


def test_correct_amount_verified():
    chunks = [_chunk("c1", "员工出差住宿标准为每晚 500元，凭发票报销。")]
    r = V.verify_answer("出差住宿标准是每晚 500元。", ["c1"], chunks)
    assert r.verdict == V.VERIFIED
    assert r.contradicted == 0


def test_wrong_amount_failed():
    """引用内容相关但金额错误：共享词重叠不得放行。"""
    chunks = [_chunk("c1", "员工出差住宿标准为每晚 500元，凭发票报销。")]
    r = V.verify_answer("出差住宿标准是每晚 800元。", ["c1"], chunks)
    assert r.verdict == V.FAILED
    assert r.contradicted >= 1
    assert any("矛盾" in p.get("problem", "") for p in r.problems)


def test_empty_citation_insufficient_not_verified():
    """空引用：有事实性答案但零引用 => 不得判为已核验。"""
    chunks = [_chunk("c1", "住宿标准 500元。")]
    r = V.verify_answer("住宿标准是 500元。", [], chunks)
    assert r.verdict in (V.INSUFFICIENT, V.FAILED)
    assert r.verdict != V.VERIFIED
    assert any("未提供任何引用" in p.get("problem", "") for p in r.problems)


def test_missing_citation_failed():
    """引用了不存在的 chunk_id => 核验失败。"""
    chunks = [_chunk("c1", "住宿标准 500元。")]
    r = V.verify_answer("标准 500元。", ["c9"], chunks)
    assert r.verdict == V.FAILED
    assert "c9" in r.missing_citations


def test_condition_reversed_failed():
    """条件相反：证据"不得"而答案"可以" => 核验失败。"""
    chunks = [_chunk("c1", "出差期间不得携带家属同行，费用不予报销。")]
    r = V.verify_answer("出差可以携带家属同行。", ["c1"], chunks)
    assert r.verdict == V.FAILED


def test_unsupported_extra_claim_partial_or_failed():
    """有效引用中夹带无据数字：有据部分通过，无据数字不得判为已核验。"""
    chunks = [_chunk("c1", "住宿标准每晚 500元。")]
    # 答案含一个证据里没有的数字（餐补 200元）
    r = V.verify_answer("住宿标准每晚 500元，餐补每天 200元。", ["c1"], chunks)
    assert r.verdict in (V.PARTIAL, V.FAILED)
    assert r.verdict != V.VERIFIED
    # 500 有据
    assert any(cl["status"] == "supported" for cl in r.claims)


def test_no_number_overlap_does_not_auto_pass():
    """词重叠高但数字不一致：不得仅凭词语重叠判正确。"""
    chunks = [_chunk("c1", "公司交通补贴标准为每月 300元。")]
    r = V.verify_answer("公司交通补贴标准为每月 900元。", ["c1"], chunks)
    assert r.verdict == V.FAILED


def test_unrelated_evidence_insufficient():
    """证据与问题无关 => 证据不足/失败，不判 verified。"""
    chunks = [_chunk("c1", "今天天气晴朗。")]
    r = V.verify_answer("住宿标准是每晚 500元。", ["c1"], chunks)
    assert r.verdict in (V.INSUFFICIENT, V.PARTIAL, V.FAILED)
    assert r.verdict != V.VERIFIED


def test_result_dict_shape():
    chunks = [_chunk("c1", "住宿 500元。")]
    r = V.verify_answer("住宿 500元。", ["c1"], chunks).to_dict()
    assert set(["verdict", "passed", "claims", "problems", "counts", "missing_citations"]) <= set(r)
    assert r["verdict"] in ("verified", "partial", "insufficient", "failed")
