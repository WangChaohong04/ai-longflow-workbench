"""问题：百度网页搜索适配器（offline，注入轻量 http 客户端）——解析 result 容器、SSRF 过滤、默认选型。"""
from __future__ import annotations

import json

from longflow import search_provider as sp
from longflow.search_provider import BaiduSearchProvider

_HTML = """<html><body>
<div class="result" mu="https://example.com/a">
 <h3 class="t"><a href="https://example.com/a">标题甲 10万元</a></h3>
 <div class="c-abstract">燃油车价格 10 万元 2024 款</div>
</div>
<div class="result" mu="http://127.0.0.1/x">
 <h3><a href="http://127.0.0.1/x">内网页</a></h3>
 <div class="c-abstract">内网摘要</div>
</div>
<div class="result">
 <h3><a href="https://example.org/b">标题乙</a></h3>
 <div class="c-abstract">第二摘要 8 万元</div>
</div>
</body></html>"""


class _Resp:
    status_code = 200


class _FakeHttp:
    def __init__(self, html):
        self._html = html.encode("utf-8")
        self.seen = {}

    def get(self, url, **kw):
        self.seen["url"] = url
        self.seen["headers"] = kw.get("headers")
        self.seen["params"] = kw.get("params")
        r = _Resp()
        r.content = self._html
        return r


def test_baidu_parses_result_blocks_and_filters_private():
    http = _FakeHttp(_HTML)
    p = BaiduSearchProvider(http, fetch_content=False)
    res = p.search("测试 查询", limit=5)
    # 内网(mu=127.0.0.1)被 SSRF 过滤；保留两个公开结果
    urls = [r.url for r in res]
    assert urls == ["https://example.com/a", "https://example.org/b"]
    assert res[0].title == "标题甲 10万元"
    assert "燃油车价格 10 万元" in res[0].snippet
    # 请求带 UA 头发往 baidu 端点
    assert http.seen["url"].startswith("https://www.baidu.com/s")
    assert "User-Agent" in (http.seen["headers"] or {})


def test_baidu_fetch_content_off_does_not_fake_body():
    http = _FakeHttp(_HTML)
    p = BaiduSearchProvider(http, fetch_content=False)
    res = p.search("q", limit=5)
    assert res and res[0].content_fetched is False and res[0].content == ""


# ---------- 正式 API（带 key）适配器 ----------

_JSON = {"results": [
    {"url": "https://example.com/api1", "title": "AP标题 12万", "snippet": "API摘要",
     "published_at": "2025-02-02"},
    {"url": "http://10.0.0.1/x", "title": "内网", "snippet": "x"},
]}


def test_baidu_api_sends_key_header_and_parses_results():
    from longflow.search_provider import BaiduApiSearchProvider
    http = _FakeHttp(json.dumps(_JSON))
    p = BaiduApiSearchProvider(http, api_key="BAIDUKEY", endpoint="https://baidu.api.example/v1/search")
    res = p.search("查询", limit=5)
    assert http.seen["headers"].get("Authorization") == "BAIDUKEY"
    assert [r.url for r in res] == ["https://example.com/api1"]  # 内网被过滤
    assert res[0].published_at == "2025-02-02"


def test_baidu_api_custom_auth_header():
    from longflow.search_provider import BaiduApiSearchProvider
    fo = _FakeHttp(json.dumps(_JSON))
    p = BaiduApiSearchProvider(fo, api_key="K", endpoint="https://api.baidu.com/v1/s",
                               auth_header="X-Baidu-Api-Key")
    p.search("q", limit=3)
    assert fo.seen["headers"].get("X-Baidu-Api-Key") == "K"


def test_default_provider_prefers_api_when_key_set():
    cfg = {"search_endpoint": "https://api.baidu.com/v1/s", "search_baidu_key": "K",
           "search_kind": "baidu_api"}
    from longflow.search_provider import BaiduApiSearchProvider
    prov = sp.default_provider(http=_FakeHttp("{}"), cfg=cfg)
    assert isinstance(prov, BaiduApiSearchProvider)


def test_default_provider_picks_page_when_no_key():
    cfg = {"search_endpoint": "https://www.baidu.com/s"}
    prov = sp.default_provider(http=_FakeHttp(_HTML), cfg=cfg)
    assert isinstance(prov, BaiduSearchProvider)


def test_default_provider_picks_baidu_by_endpoint_host():
    cfg = {"search_endpoint": "https://www.baidu.com/s", "search_fetch_content": False}
    prov = sp.default_provider(http=_FakeHttp(_HTML), cfg=cfg)
    assert isinstance(prov, BaiduSearchProvider)