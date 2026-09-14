"""P6：multipart 上传 -> 解析预览 -> 确认激活 -> 旧版归档 -> workspace 隔离。"""
from __future__ import annotations

import io as _io

from longflow import api as api_mod, config as cfg_mod
from longflow import importer
from fastapi.testclient import TestClient


def _client(workdir, tokens=None):
    cfg = cfg_mod.load_config()
    cfg["db_path"] = str(workdir / "up.db")
    if tokens:
        cfg["auth"] = {"tokens": tokens}
    return TestClient(api_mod.create_app(cfg))


def _multipart(filename: str, data: bytes, fields: dict | None = None):
    """手工构造 multipart/form-data（测试不依赖 python-multipart）。"""
    boundary = "----lfboundary123"
    buf = _io.BytesIO()
    def w(s):
        buf.write(s if isinstance(s, bytes) else s.encode("utf-8"))
    for k, v in (fields or {}).items():
        w(f"--{boundary}\r\n")
        w(f'Content-Disposition: form-data; name="{k}"\r\n\r\n')
        w(f"{v}\r\n")
    w(f"--{boundary}\r\n")
    w(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n')
    w("Content-Type: application/octet-stream\r\n\r\n")
    buf.write(data)
    w("\r\n")
    w(f"--{boundary}--\r\n")
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def test_upload_md_preview_not_searchable_then_activate(workdir):
    c = _client(workdir)
    body, ct = _multipart("policy.md", "# 云采购\n云服务采购上限 31337 元。".encode("utf-8"),
                          {"domain": "team_ops", "version": "v9"})
    r = c.post("/api/knowledge/upload", content=body, headers={"Content-Type": ct})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "preview" and d["version"] == "v9"
    assert d["section_count"] >= 1 and d["uploader"] == "local"
    doc_id = d["doc_id"]
    # 预览不参与检索
    pre = c.get("/api/knowledge", params={"q": "云服务采购上限", "domain": "team_ops"}).json()
    assert not any("31337" in (h.get("text") or "") for h in pre["results"])
    # 激活后可检索
    act = c.post(f"/api/knowledge/docs/{doc_id}/activate", json={})
    assert act.status_code == 200 and act.json()["chunks"] >= 1
    post = c.get("/api/knowledge", params={"q": "云服务采购上限", "domain": "team_ops"}).json()
    assert any("31337" in (h.get("text") or "") for h in post["results"])


def test_upload_txt_and_json(workdir):
    c = _client(workdir)
    b, ct = _multipart("note.txt", "专项额度 9001 元".encode("utf-8"), {"domain": "team_ops"})
    assert c.post("/api/knowledge/upload", content=b, headers={"Content-Type": ct}).status_code == 200
    payload = '{"title":"结构化","chunks":[{"section":"S","text":"备用金 4242 元"}]}'.encode()
    b2, ct2 = _multipart("data.json", payload, {"domain": "team_ops"})
    r2 = c.post("/api/knowledge/upload", content=b2, headers={"Content-Type": ct2})
    assert r2.status_code == 200 and r2.json()["section_count"] == 1


def test_upload_bad_extension_422(workdir):
    c = _client(workdir)
    b, ct = _multipart("virus.exe", b"MZ", {"domain": "team_ops"})
    r = c.post("/api/knowledge/upload", content=b, headers={"Content-Type": ct})
    assert r.status_code == 422


def test_upload_archives_old_version(workdir):
    c = _client(workdir)
    def up(content):
        b, ct = _multipart("same.md", content.encode("utf-8"), {"domain": "team_ops"})
        doc = c.post("/api/knowledge/upload", content=b, headers={"Content-Type": ct}).json()
        c.post(f"/api/knowledge/docs/{doc['doc_id']}/activate", json={})
        return doc["doc_id"]
    up("# 标题\n上限 100 元")
    up("# 标题\n上限 200 元")
    docs = c.get("/api/knowledge/docs", params={"domain": "team_ops"}).json()["docs"]
    same = [d for d in docs if d["title"] == "标题"]
    assert any(d["status"] == "active" for d in same)
    assert any(d["status"] == "archived" for d in same)


def test_upload_isolated_between_workspaces(workdir):
    tok = {
        "a": {"user": "alice", "role": "member", "workspaces": ["ws-a"],
              "default_workspace": "ws-a"},
        "b": {"user": "bob", "role": "member", "workspaces": ["ws-b"],
              "default_workspace": "ws-b"},
    }
    c = _client(workdir, tok)
    ha, hb = {"Authorization": "Bearer a"}, {"Authorization": "Bearer b"}
    b, ct = _multipart("secret.md", "机密额度 88888 元".encode("utf-8"),
                       {"domain": "team_ops"})
    assert c.post("/api/knowledge/upload", content=b, headers={**ha, "Content-Type": ct}).status_code == 200
    # bob 看不到 alice 的文档
    docs_b = c.get("/api/knowledge/docs", headers=hb, params={"domain": "team_ops"}).json()["docs"]
    assert not any("88888" in (d.get("title") or "") for d in docs_b)


def test_docx_pdf_without_library_reports_error(workdir):
    # 缺 python-docx/pypdf 时：明确报错（422），而不是静默当作成功
    c = _client(workdir)
    b, ct = _multipart("a.docx", b"PK\x03\x04fake", {"domain": "team_ops"})
    r = c.post("/api/knowledge/upload", content=b, headers={"Content-Type": ct})
    # 有库则可能解析成功（真实环境）；无库必须是 422 而非 200 空内容
    if r.status_code == 200:
        import importlib.util
        assert importlib.util.find_spec("docx") is not None
    else:
        assert r.status_code == 422
