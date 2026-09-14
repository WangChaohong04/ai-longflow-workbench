"""批次4：RAG 文件导入——解析/预览/确认激活/旧版归档/隔离。"""
from __future__ import annotations

import pytest

from longflow import db, rag
from longflow import importer as IMP


def _conn(workdir):
    conn = db.connect(str(workdir / "imp.db"))
    db.init_db(conn)
    rag._ensure_rag_cols(conn)
    return conn


def test_parse_md_sections_and_version():
    doc = IMP.parse_text("policy.md", "# 差旅政策\n版本 2025\n## 住宿\n标准 500 元\n\n## 交通\n高铁二等座")
    assert doc.title == "差旅政策"
    assert doc.version == "2025"
    secs = [s["section"] for s in doc.sections]
    assert "住宿" in secs and "交通" in secs


def test_unsupported_extension_rejected():
    with pytest.raises(ValueError):
        IMP.parse_text("virus.exe", b"binary")


def test_preview_not_searchable_until_activated(workdir):
    conn = _conn(workdir)
    doc = IMP.parse_text("kb.md", "# 专属政策\n## 额度\n量子通讯设备采购上限 12345 元")
    doc_id = IMP.preview(conn, workspace="ws1", owner="u1", domain="team_ops", doc=doc)
    # 预览状态：检索不到
    hits = rag.search(conn, "量子通讯设备 12345", scenario="user:ws1:team_ops")
    assert all("量子通讯" not in (c.get("text") or "") for c in hits)
    # 用户确认后激活
    n = IMP.activate(conn, doc_id, by="u1")
    assert n >= 1
    hits2 = rag.search(conn, "量子通讯设备采购上限", scenario="user:ws1:team_ops")
    assert any("12345" in (c.get("text") or "") for c in hits2)


def test_workspace_domain_isolation(workdir):
    conn = _conn(workdir)
    doc = IMP.parse_text("a.md", "# 内部资料\n## 机密\n项目代号 蓝鹰 预算 999")
    doc_id = IMP.preview(conn, workspace="ws-secret", owner="u1", domain="team_ops", doc=doc)
    IMP.activate(conn, doc_id, by="u1")
    # 不同 workspace 的 scenario 检索不到
    other = rag.search(conn, "蓝鹰 项目代号", scenario="user:ws-other:team_ops")
    assert all("蓝鹰" not in (c.get("text") or "") for c in other)
    # 同 workspace 可检索
    same = rag.search(conn, "蓝鹰 项目代号", scenario="user:ws-secret:team_ops")
    assert any("蓝鹰" in (c.get("text") or "") for c in same)


def test_old_version_archived_on_reimport(workdir):
    conn = _conn(workdir)
    d1 = IMP.parse_text("policy.md", "# 制度\n版本 v1\n## x\n旧规则 内容AAA")
    id1 = IMP.preview(conn, workspace="ws", owner="u", domain="team_ops", doc=d1)
    IMP.activate(conn, id1, by="u")
    d2 = IMP.parse_text("policy.md", "# 制度\n版本 v2\n## x\n新规则 内容BBB")
    id2 = IMP.preview(conn, workspace="ws", owner="u", domain="team_ops", doc=d2)
    IMP.activate(conn, id2, by="u")
    docs = {d["id"]: d["status"] for d in IMP.list_docs(conn, workspace="ws")}
    assert docs[id1] == IMP.STATUS_ARCHIVED
    assert docs[id2] == IMP.STATUS_ACTIVE
    # 检索命中新版内容
    hits = rag.search(conn, "规则 内容", scenario="user:ws:team_ops")
    assert any("内容BBB" in (c.get("text") or "") for c in hits)


def test_preview_summary_shape(workdir):
    conn = _conn(workdir)
    doc = IMP.parse_text("p.md", "# T\n## S1\n正文内容若干。")
    doc_id = IMP.preview(conn, workspace="ws", owner="u", domain="d", doc=doc)
    s = IMP.preview_summary(conn, doc_id)
    assert s["status"] == IMP.STATUS_PREVIEW and s["section_count"] >= 1
    assert s["preview"][0]["text"]
