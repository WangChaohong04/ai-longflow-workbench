"""问题3：搜索→正文抓取→字段抽取→(下游标准化/核验/对比的数据源)。

验收：fetch_content 真正抓正文（摘要与正文区分）；受 SSRF/白名单/重定向/大小限制；
按任务字段提取结构化值并保留原文与来源；未配置/无结果/访问失败分别报告。
"""
from __future__ import annotations

import pytest

from longflow import evidence as E
from longflow import subagents as sa
from longflow.search_provider import (
    HttpSearchProvider, NullSearchProvider, SearchResult)
from longflow.subagent_runner import SubagentRunner


class _Resp:
    def __init__(self, *, json_data=None, content=b"", headers=None, status=200,
                 is_redirect=False):
        self.json_data = json_data
        self.content = content
        self.headers = headers or {}
        self.status_code = status
        self.is_redirect = is_redirect

    def json(self):
        return self.json_data or {}


class _FakeHttp:
    def __init__(self, endpoint, results, pages):
        self.endpoint = endpoint
        self.results = results
        self.pages = pages
        self.gets = []

    def get(self, url, params=None, timeout=None, follow_redirects=False):
        self.gets.append(url)
        if url == self.endpoint:
            return _Resp(json_data=self.results, status=200)
        html = self.pages.get(url)
        if html is None:
            return _Resp(content=b"", status=404)
        return _Resp(content=html.encode("utf-8"), status=200)


PAGE = "<html><body><h1>新能源车A</h1><p>官方续航 {range}，售价 {price}，纯电</p></body></html>"


def _provider(http, endpoint, results, pages, *, fetch_content=True):
    return HttpSearchProvider(http, endpoint, fetch_content=fetch_content,
                              allowlist=["example.com"])


def test_fetch_content_fetches_body_and_distinguishes_snippet():
    endpoint = "https://api.example.com/search"
    results = {"results": [{"url": "https://example.com/a", "title": "车A",
                            "snippet": "摘要：续航与售价",
                            "published_at": "2024-09-01"}]}
    pages = {"https://example.com/a": PAGE.format(range="1500 米", price="10万元")}
    http = _FakeHttp(endpoint, results, pages)
    # 开启 fetch_content
    prov = _provider(http, endpoint, results, pages, fetch_content=True)
    res = prov.search("纯电 续航")
    assert res and res[0].content_fetched is True
    assert "1500" in res[0].content and "米" in res[0].content  # 正文来自页面
    assert "摘要" in res[0].snippet  # 摘要仍保留，二者分离
    # 关闭 fetch_content：只用摘要，不抓正文
    prov2 = _provider(http, endpoint, results, pages, fetch_content=False)
    res2 = prov2.search("纯电 续航")
    assert res2[0].content_fetched is False and res2[0].content == ""


def test_field_extraction_from_fetched_body():
    endpoint = "https://api.example.com/search"
    results = {"results": [{"url": "https://example.com/a", "title": "车A",
                            "snippet": "s", "published_at": "2024-09-01"}]}
    pages = {"https://example.com/a": PAGE.format(range="1500 米", price="10万元")}
    http = _FakeHttp(endpoint, results, pages)
    prov = _provider(http, endpoint, results, pages, fetch_content=True)
    r = SubagentRunner(runtime=None, provider=prov)
    res = r.run(sa.SA_WEB_RESEARCHER,
                sa.SubagentRequest(query="纯电 续航", required_fields=["range", "price"],
                                   allowed_domains=["example.com"]),
                task_id="t", root_id="t")
    assert res.ok
    ext = [e for e in res.evidence if e.get("field") in ("range", "price")]
    assert ext, "未抽取任何结构化字段"
    range_hit = next((e for e in ext if e["field"] == "range"), None)
    assert range_hit is not None
    assert float(range_hit["value"]) == 1500
    assert range_hit["unit"] == "m"
    assert "1500" in (range_hit["evidence_text"] or "")
    assert range_hit["source_url"] == "https://example.com/a"
    # 网页正则抽取为"未确认"，不冒充已核验事实
    assert range_hit["layer"] == E.LAYER_UNCONFIRMED


def test_ssrf_and_allowlist_guards():
    from longflow import netguard
    # check_url 直接判白名单外/私网/非法 scheme
    with pytest.raises(netguard.UrlNotAllowed):
        netguard.check_url("http://127.0.0.1/x", allowlist=["example.com"])
    with pytest.raises(netguard.UrlNotAllowed):
        netguard.check_url("file:///etc/passwd", allowlist=["example.com"])
    with pytest.raises(netguard.UrlNotAllowed):
        netguard.check_url("https://evil.example.net/x", allowlist=["example.com"])
    # 重定向到白名单外 -> 正文抓取被拒, 不产生内容
    class _RedirHttp:
        def get(self, url, timeout=None, follow_redirects=False):
            return _Resp(is_redirect=True,
                         headers={"location": "https://evil.example.net/x"})
    prov = HttpSearchProvider(_RedirHttp(), "https://api.example.com/search",
                              fetch_content=True, allowlist=["example.com"])
    body, err = prov._fetch_page("https://example.com/a")
    assert body == "" and err == "redirect_denied"


def test_error_modes_distinct():
    # 未配置后端
    r = SubagentRunner(runtime=None, provider=NullSearchProvider())
    res = r.run(sa.SA_WEB_RESEARCHER, sa.SubagentRequest(query="x"),
                task_id="t", root_id="t")
    assert res.error == "source_not_configured"

    # 配置了后端但无结果
    class _Empty:
        name = "http"
        def search(self, *a, **k):
            return []
    r2 = SubagentRunner(runtime=None, provider=_Empty())
    res2 = r2.run(sa.SA_WEB_RESEARCHER, sa.SubagentRequest(query="x"),
                  task_id="t", root_id="t")
    assert res2.error == "source_unreachable"

    # 后端访问失败（抛异常）
    class _Boom:
        name = "http"
        def search(self, *a, **k):
            raise RuntimeError("conn refused")
    r3 = SubagentRunner(runtime=None, provider=_Boom())
    res3 = r3.run(sa.SA_WEB_RESEARCHER, sa.SubagentRequest(query="x"),
                  task_id="t", root_id="t")
    assert res3.error.startswith("search_unreachable")