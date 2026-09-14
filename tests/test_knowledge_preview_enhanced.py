"""问题6b：可见知识预览——解析内容/分段/页码/版本/警告、粘贴文本、检索试问/引用。"""
from __future__ import annotations

import io
import zipfile

import pytest

from longflow import db, importer as IMP


def _conn(workdir):
    conn = db.connect(str(workdir / "preview.db"))
    db.init_db(conn)
    return conn


def test_full_preview_content_version_warnings(workdir):
    conn = _conn(workdir)
    doc = IMP.parse_text("policy.md", "# 差旅政策\n版本 2025\n## 住宿\n标准 500 元\n## 交通\n高铁")
    doc.warnings.append("测试警告：可能为扫描件")
    doc_id = IMP.preview(conn, workspace="ws", owner="u", domain="d", doc=doc)
    s = IMP.preview_summary(conn, doc_id, full=True)
    # 完整内容：不只段数
    assert s["section_count"] >= 2
    texts = "".join(x["text"] or "" for x in s["preview"])
    assert "标准 500 元" in texts and "高铁" in texts
    assert s["version"] == "2025"
    assert s["warnings"] == ["测试警告：可能为扫描件"]
    assert any(x["page"] is None or isinstance(x["page"], int) for x in s["preview"])


def test_paste_text_preview_via_import(workdir):
    conn = _conn(workdir)
    doc = IMP.parse_text("pasted.txt", "用户直接粘贴：内部接入标准 800 元。")
    doc_id = IMP.preview(conn, workspace="ws", owner="u", domain="team_ops", doc=doc)
    s = IMP.preview_summary(conn, doc_id, full=True)
    assert "内部接入标准" in s["preview"][0]["text"]
    assert s["status"] == IMP.STATUS_PREVIEW  # 未激活，不参与检索


def test_trial_query_returns_hits_with_citation(workdir):
    conn = _conn(workdir)
    doc = IMP.parse_text("kb.md", "# 制度\n## 采购\n设备预算上限 9999 元，走审批\n## 报销\n票据要求")
    doc_id = IMP.preview(conn, workspace="ws", owner="u", domain="team_ops", doc=doc)
    out = IMP.preview_trial_query(conn, doc_id, "预算 上限 采购", top_k=3)
    assert out["hit_count"] >= 1
    hit = out["hits"][0]
    assert "预算上限 9999" in hit["excerpt"] or "预算上限" in hit["excerpt"]
    # 引用携带文档/来源/分段/版本
    assert hit["citation"]["doc"] == "制度"
    assert hit["citation"]["filename"] == "kb.md"
    assert hit["citation"]["section"]


def _docx_with_table_raw() -> bytes:
    xml = (
        '<?xml version="1.0"?>'
        '<w:document xmlns:w="urn:x">'
        '<w:body>'
        '<w:p><w:r><w:t>标题段落</w:t></w:r></w:p>'
        '<w:tbl>'
        '<w:tr><w:tc><w:p><w:r><w:t>型号</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>X200</w:t></w:r></w:p></w:tc></w:tr>'
        '</w:tbl>'
        '</w:body></w:document>'
    )
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return z.getvalue()


def test_docx_table_text_extracted():
    # 真实 OOXML：无 python-docx 时走 zip 回退，仍提取段落与表格单元格文本
    raw = _docx_with_table_raw()
    doc = IMP.parse_docx("spec.docx", raw)  # 无 python-docx 依赖
    text = "".join(s["text"] for s in doc.sections)
    assert "标题段落" in text
    assert "型号" in text and "X200" in text  # 表格内容不被忽略


def test_pdf_clear_failure_or_real_when_lib_present():
    import importlib.util
    if importlib.util.find_spec("pypdf") or importlib.util.find_spec("PyPDF2"):
        pytest.skip("pypdf 已安装：由真实 PDF 回归覆盖，此处仅验证缺库明确失败路径")
    with pytest.raises(ValueError) as e:
        IMP.parse_pdf("scan.pdf", b"%PDF-1.4 fake")
    assert "pypdf" in str(e.value) or "PyPDF2" in str(e.value)