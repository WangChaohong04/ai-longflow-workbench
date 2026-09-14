"""P1：http_get 重定向绕过与域名白名单（用桩 http client）。"""
from __future__ import annotations

import types

from longflow import tools, db
from tests.conftest import tmp_db


class _Resp:
    def __init__(self, status_code, location=None, text="", url="https://safe.gov/x"):
        self.status_code = status_code
        self.headers = {"location": location} if location else {}
        self.text = text
        self.url = types.SimpleNamespace(scheme="https", host="safe.gov")


class _Http:
    def __init__(self, resp):
        self._resp = resp
    def get(self, url, timeout=15, follow_redirects=False):
        return self._resp


def _run(s, http, url, allow=None):
    s.runtime.http = http
    spec = s.registry.get("http_get")
    from longflow.plugins.sdk import ToolContext
    ctx = ToolContext(conn=s.conn, task_id="t", root_id="t", scenario="",
                      emit=lambda *a, **k: None, http=http, data_dir="",
                      plugin_config={"allowed_domains": allow} if allow else {})
    return spec.handler({"url": url, "allowed_domains": allow}, ctx)


def test_redirect_to_metadata_blocked(workdir):
    s = tmp_db(workdir)
    http = _Http(_Resp(302, location="http://169.254.169.254/latest/meta-data/"))
    out = _run(s, http, "https://safe.gov/x")
    assert out["ok"] is False and out.get("blocked") is True


def test_redirect_off_whitelist_blocked(workdir):
    s = tmp_db(workdir)
    http = _Http(_Resp(302, location="https://evil.com/leak"))
    out = _run(s, http, "https://safe.gov/x", allow=["safe.gov"])
    assert out["ok"] is False and out.get("blocked") is True


def test_non_whitelist_domain_blocked_without_request(workdir):
    s = tmp_db(workdir)
    called = {"n": 0}
    class Trap:
        def get(self, *a, **k):
            called["n"] += 1
            raise AssertionError("不应发起请求")
    out = _run(s, Trap(), "https://evil.com/x", allow=["safe.gov"])
    assert out["ok"] is False and out.get("blocked") is True and called["n"] == 0


def test_whitelisted_200_returns_text(workdir):
    s = tmp_db(workdir)
    http = _Http(_Resp(200, text="官方正文"))
    out = _run(s, http, "https://safe.gov/x", allow=["safe.gov"])
    assert out["ok"] is True and "官方正文" in out["text"]
