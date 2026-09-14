"""可替换的搜索 Provider 接口（研究型 Subagent 用）。

web/official/forum researcher 不能只抓站点首页：它们按 **query + 目标域名 +
时间范围 + 返回字段** 检索"结果页"，再提取正文。Provider 可替换：

- ``SearchProvider`` 抽象接口：``search(query, domains, time_range, fields, limit)
  -> list[SearchResult]``；
- ``NullSearchProvider``：未配置任何后端时使用——**不臆造结果**，返回空 + 不可用标记，
  使该来源在任务图中表现为"来源未配置/不可达"（可部分失败），不伪造网页；
- ``HttpSearchProvider``：对管理员配置的搜索端点发 GET（受 netguard SSRF/白名单约束），
  解析 JSON 结果并按需抓取结果页正文（同样过 SSRF/白名单）；
- 真实环境可注入实现了同接口的官方/授权 API Provider（如企业搜索、政府开放 API）。

所有结果统一映射为 SearchResult，再由 SubagentRunner 转成 EvidenceRecord。
"""
from __future__ import annotations

import dataclasses
import json
import re
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit


@dataclasses.dataclass
class SearchResult:
    url: str
    title: str
    snippet: str = ""
    content: str = ""              # 提取的正文（可能为空，调用方据此判定证据完整度）
    domain: str = ""
    published_at: str | None = None
    source_kind: str = "web"       # web | official | forum
    content_fetched: bool = False  # content 是否来自真正抓取的正文（否则仅为摘要/接口返回）
    collected_at: str = dataclasses.field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    extra: dict = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class SearchProvider(Protocol):
    name: str
    def search(self, query: str, *, domains: list[str] | None = None,
               time_range: str | None = None, fields: list[str] | None = None,
               limit: int = 8, kind: str = "web") -> list[SearchResult]: ...


class NullSearchProvider:
    """无后端：明确不可用，不返回任何伪造结果。"""
    name = "null"

    def search(self, query: str, *, domains=None, time_range=None, fields=None,
               limit: int = 8, kind: str = "web") -> list[SearchResult]:
        return []  # 调用方据 provider.name=='null' 标注 source_not_configured


class HttpSearchProvider:
    """对可配置搜索端点发请求的最小实现（只读）。

    config:
      endpoint: 搜索 API 地址（返回 {"results":[{url,title,snippet,published_at,...}]}）
      fetch_content: 是否再抓取结果页正文（默认 False，只使用搜索摘要）
      max_content_bytes: 单页正文大小上限（默认 200KB），超出截断并标记
    所有出站请求经 netguard 校验（SSRF + 域名白名单）；endpoint 必须在允许域内；
    结果页抓取同样过白名单，并校验重定向目标域仍在白名单。
    """
    def __init__(self, http, endpoint: str = "", *, fetch_content: bool = False,
                 allowlist: list[str] | None = None, max_content_bytes: int = 200_000):
        self.http = http
        self.endpoint = endpoint
        self.fetch_content = fetch_content
        self.allowlist = allowlist or []
        self.max_content_bytes = int(max_content_bytes or 200_000)
        self.name = "http" if endpoint else "null"

    def _fetch_page(self, url: str) -> tuple[str, str]:
        """抓取单页正文：SSRF/白名单 + 重定向校验 + 超时 + 大小上限。

        返回 (正文纯文本, error)。error 非空表示未取到正文，调用方如实标注。
        """
        from . import netguard
        try:
            netguard.check_url(url, allowlist=self.allowlist or None)
        except netguard.UrlNotAllowed as exc:
            return "", f"url_denied:{exc}"
        cur = url
        for _ in range(5):
            if self.http is None:
                return "", "no_http_client"
            try:
                resp, body = self._get(cur)
            except Exception as exc:  # noqa: BLE001
                return "", f"fetch_failed:{str(exc)[:120]}"
            if getattr(resp, "is_redirect", False):
                loc = (resp.headers or {}).get("location")
                if not loc:
                    return "", "redirect_without_location"
                cur = urljoin(cur, loc)
                try:
                    netguard.check_url(cur, allowlist=self.allowlist or None)
                except netguard.UrlNotAllowed:
                    return "", "redirect_denied"
                continue
            if not 200 <= resp.status_code < 300:
                return "", f"http_status:{resp.status_code}"
            return _html_to_text(body), ""
        return "", "too_many_redirects"

    def _get(self, url, params=None, headers=None):
        """Production reads are bounded while streaming, including decoded response bytes."""
        if hasattr(self.http, "stream"):
            with self.http.stream("GET", url, params=params, headers=headers, timeout=15,
                                  follow_redirects=False) as resp:
                body = bytearray()
                for chunk in resp.iter_bytes():
                    if len(body) + len(chunk) > self.max_content_bytes:
                        raise ValueError("search_response_too_large")
                    body.extend(chunk)
                return resp, bytes(body)
        # Lightweight injected test/connector clients must supply bounded bodies.
        kwargs = {"timeout": 15, "follow_redirects": False}
        if params is not None:
            kwargs["params"] = params
        if headers is not None:
            kwargs["headers"] = headers
        resp = self.http.get(url, **kwargs)
        body = getattr(resp, "content", b"") or b""
        if len(body) > self.max_content_bytes:
            raise ValueError("search_response_too_large")
        return resp, body

    def search(self, query: str, *, domains=None, time_range=None, fields=None,
               limit: int = 8, kind: str = "web") -> list[SearchResult]:
        from . import netguard
        if not self.endpoint or self.http is None:
            return []
        netguard.check_url(self.endpoint, allowlist=self.allowlist or None)
        params = {"q": query, "limit": limit}
        if domains:
            params["domains"] = ",".join(domains)
        if time_range:
            params["time_range"] = time_range
        params["kind"] = kind
        if fields:
            params["fields"] = ",".join(fields)
        resp, body = self._get(self.endpoint, params=params)
        if not 200 <= getattr(resp, "status_code", 0) < 300:
            raise RuntimeError(f"search_http_status:{resp.status_code}")
        data = json.loads(body) if body else resp.json()
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ValueError("search_invalid_response")
        out: list[SearchResult] = []
        for item in (data.get("results", []) if isinstance(data, dict) else [])[:limit]:
            if not isinstance(item, dict):
                continue
            url = item.get("url", "")
            try:
                netguard.check_url(url, allowlist=self.allowlist or None)
                if domains:
                    netguard.check_url(url, allowlist=domains)
            except netguard.UrlNotAllowed:
                continue
            out.append(SearchResult(
                url=url, title=item.get("title", "") or url,
                snippet=item.get("snippet", "") or "",
                content=item.get("content", "") or "",
                domain=item.get("domain", ""),
                published_at=item.get("published_at"),
                source_kind=kind,
                extra={k: v for k, v in item.items()
                       if k not in {"url", "title", "snippet", "content",
                                    "domain", "published_at"}}))
        self._enrich_content(out)
        return out

    def _enrich_content(self, out: list[SearchResult]) -> None:
        """fetch_content 生效：真正抓取结果页正文（摘要与正文分离，正文带来源）。"""
        if not self.fetch_content:
            return
        for res in out:
            body, err = self._fetch_page(res.url)
            if body:
                res.content = body
                res.content_fetched = True
            else:
                res.extra["fetch_error"] = err or "fetch_failed"
                res.extra["used_snippet_only"] = True



_BAIDU_ENDPOINT = "https://www.baidu.com/s"


class BaiduSearchProvider(HttpSearchProvider):
    """百度网页搜索（www.baidu.com/s）适配器——免 API key。

    - GET 搜索页并带 UA，解析结果容器（优先容器 ``mu`` 实链，其次 h3 内 ``a[href]``），
      摘要 = 该容器去标签后的纯文本前若干字；
    - 端点固定允许 www.baidu.com；结果 URL / 正文抓取仍走 netguard（SSRF / 私有 IP 过滤）；
    - 说明：百度无官方 JSON、属页面抓取——反爬 / 限流 / 登录墙可能致空结果，如实返回空
      不臆造；正文抓取遵循 fetch_content 开关（摘要与正文分离）。
    """

    def __init__(self, http, endpoint: str = _BAIDU_ENDPOINT, *,
                 fetch_content: bool = False, allowed_domains: list[str] | None = None,
                 max_content_bytes: int = 200_000):
        super().__init__(http, endpoint or _BAIDU_ENDPOINT,
                         fetch_content=fetch_content,
                         allowlist=list(allowed_domains) if allowed_domains else None,
                         max_content_bytes=max_content_bytes)
        self.name = "baidu"

    def search(self, query: str, *, domains=None, time_range=None, fields=None,
               limit: int = 8, kind: str = "web") -> list[SearchResult]:
        import html as _html
        from . import netguard
        if not self.endpoint or self.http is None:
            return []
        netguard.check_url(self.endpoint, allowlist=["www.baidu.com", "baidu.com"])
        params = {"wd": query, "rn": max(1, min(int(limit), 50)), "ie": "utf-8"}
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
        resp, body = self._get(self.endpoint, params=params,
                               headers={"User-Agent": ua,
                                        "Accept-Language": "zh-CN,zh;q=0.9"})
        if not 200 <= getattr(resp, "status_code", 0) < 300:
            raise RuntimeError(f"search_http_status:{resp.status_code}")
        text = (body or b"").decode("utf-8", errors="ignore")
        chunks = re.split(r'(?=<div[^>]*class="[^"]*\bresult\b)', text)
        out: list[SearchResult] = []
        for chunk in chunks[1:]:
            if len(out) >= limit:
                break
            mu = re.search(r'\bmu="([^"]+)"', chunk)
            m = re.search(r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                          chunk, re.S)
            url = mu.group(1) if mu else (m.group(1) if m else "")
            if not url or not url.startswith(("http://", "https://")):
                continue
            title = _html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip() if m else ""
            try:
                netguard.check_url(url, allowlist=self.allowlist or None)
                if domains:
                    netguard.check_url(url, allowlist=domains)
            except netguard.UrlNotAllowed:
                continue
            snippet = _html_to_text(chunk, limit=4000)
            out.append(SearchResult(
                url=url, title=title or url, snippet=snippet, content="",
                domain=urlsplit(url).hostname or "",
                published_at=None, source_kind=kind, extra={}))
        self._enrich_content(out)
        return out


class BaiduApiSearchProvider(HttpSearchProvider):
    """百度正式搜索 API 适配器（带 key，服务端读取，仅内存）。

    - 端点为管理员配置的正式 API 地址；请求头 ``{auth_header: api_key}``，
      auth_header 默认 ``Authorization``，可经 cfg search_baidu_header 改；
    - 期望返回与 HttpSearchProvider 相同的 JSON 契约：``{"results":[{url,title,
      snippet,published_at,...}]}``（若你申请的百度 API 字段不同，告诉我实际结构，
      我们按它改这一处解析）；
    - 端点/结果 URL / 正文抓取一律走 netguard（SSRF / 私有 IP 过滤）。
    """

    def __init__(self, http, api_key: str, endpoint: str, *,
                 auth_header: str = "Authorization", fetch_content: bool = False,
                 allowed_domains: list[str] | None = None,
                 max_content_bytes: int = 200_000):
        super().__init__(http, endpoint,
                         fetch_content=fetch_content,
                         allowlist=list(allowed_domains) if allowed_domains else None,
                         max_content_bytes=max_content_bytes)
        self.api_key = api_key
        self.auth_header = auth_header or "Authorization"
        self.name = "baidu_api"

    def search(self, query: str, *, domains=None, time_range=None, fields=None,
               limit: int = 8, kind: str = "web") -> list[SearchResult]:
        from . import netguard
        if not self.endpoint or self.http is None or not self.api_key:
            return []
        netguard.check_url(self.endpoint, allowlist=self.allowlist or None)
        params = {"q": query, "limit": max(1, min(int(limit), 50)), "kind": kind}
        if time_range:
            params["time_range"] = time_range
        resp, body = self._get(self.endpoint, params=params,
                               headers={self.auth_header: self.api_key})
        if not 200 <= getattr(resp, "status_code", 0) < 300:
            raise RuntimeError(f"search_http_status:{resp.status_code}")
        data = json.loads(body) if body else resp.json()
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ValueError("search_invalid_response")
        out: list[SearchResult] = []
        for item in data.get("results", [])[:limit]:
            if not isinstance(item, dict):
                continue
            url = item.get("url", "")
            try:
                netguard.check_url(url, allowlist=self.allowlist or None)
                if domains:
                    netguard.check_url(url, allowlist=domains)
            except netguard.UrlNotAllowed:
                continue
            out.append(SearchResult(
                url=url, title=item.get("title", "") or url,
                snippet=item.get("snippet", "") or "",
                content=item.get("content", "") or "",
                domain=item.get("domain", "") or url,
                published_at=item.get("published_at"),
                source_kind=kind,
                extra={k: v for k, v in item.items()
                       if k not in {"url", "title", "snippet", "content",
                                    "domain", "published_at"}}))
        self._enrich_content(out)
        return out


def default_provider(http=None, cfg: dict | None = None) -> SearchProvider:
    cfg = cfg or {}
    endpoint = cfg.get("search_endpoint", "")
    bkey = cfg.get("search_baidu_key", "")
    kind = cfg.get("search_kind", "")  # ""|baidu_page|baidu_api
    if not endpoint or http is None:
        return NullSearchProvider()
    host = urlsplit(endpoint).hostname or ""
    is_baidu = "baidu.com" in host
    if is_baidu and bkey and kind != "baidu_page":
        # 有 key → 百度正式 API
        return BaiduApiSearchProvider(
            http, bkey, endpoint,
            auth_header=cfg.get("search_baidu_header", "Authorization"),
            fetch_content=bool(cfg.get("search_fetch_content")),
            allowed_domains=cfg.get("allowed_domains"),
            max_content_bytes=int(cfg.get("search_max_content_bytes", 200_000)))
    if is_baidu:
        # 无 key（或显式 baidu_page）→ 免 key 页面抓取
        return BaiduSearchProvider(
            http, endpoint,
            fetch_content=bool(cfg.get("search_fetch_content")),
            allowed_domains=cfg.get("allowed_domains"),
            max_content_bytes=int(cfg.get("search_max_content_bytes", 200_000)))
    if bkey and kind == "baidu_api":
        # 用户的正式 API 不在 baidu.com 域，但显式声明走 API（端点仍过 netguard）
        return BaiduApiSearchProvider(
            http, bkey, endpoint,
            auth_header=cfg.get("search_baidu_header", "Authorization"),
            fetch_content=bool(cfg.get("search_fetch_content")),
            allowed_domains=cfg.get("allowed_domains"),
            max_content_bytes=int(cfg.get("search_max_content_bytes", 200_000)))
    if cfg.get("allowed_domains"):
        return HttpSearchProvider(http, endpoint,
                                  fetch_content=bool(cfg.get("search_fetch_content")),
                                  allowlist=cfg.get("allowed_domains"),
                                  max_content_bytes=int(cfg.get("search_max_content_bytes", 200_000)))
    return NullSearchProvider()


def _html_to_text(html, limit: int = 200_000) -> str:
    """粗略 HTML→纯文本（去脚本/样式/标签），用于正文抽取。"""
    if not html:
        return ""
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="ignore")
    html = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]
