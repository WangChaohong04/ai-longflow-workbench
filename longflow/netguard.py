"""出站网络安全防护（SSRF / 域名白名单）。

http_get 等外部访问工具在发起请求前必须过 check_url：
- 仅允许 http/https（禁止 file://、gopher://、ftp:// 等）；
- 禁止 localhost、环回、私网、链路本地、元数据地址（169.254.169.254 等）；
- 可选域名白名单（领域包/管理员配置）；
- 调用方必须禁用重定向或对每一跳重定向目标重新校验，防止重定向绕过。
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}

# 显式禁止的主机名
_BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata", "metadata.azure.com"}
_METADATA_IPS = {
    "169.254.169.254",   # AWS / GCP / Azure 链路本地元数据
    "fd00:ec2::254",
}


class UrlNotAllowed(ValueError):
    pass


def _is_blocked_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
        or str(addr) in _METADATA_IPS
    )


def host_allowed(host: str, allowlist: list[str] | None = None) -> bool:
    """域名白名单：空/None 表示不限制（但仍做 SSRF 私网拦截）。支持后缀匹配。"""
    if not allowlist:
        return True
    host = (host or "").lower().strip(".")
    for d in allowlist:
        d = d.lower().strip("/")
        if host == d or host.endswith("." + d):
            return True
    return False


def check_url(url: str, *, allowlist: list[str] | None = None,
              resolve: bool = True) -> None:
    """校验 URL；不合法抛 UrlNotAllowed。"""
    if not url or not isinstance(url, str):
        raise UrlNotAllowed("空 URL")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise UrlNotAllowed(f"仅允许 http/https，拒绝 scheme: {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise UrlNotAllowed("缺少主机名")
    if host in _BLOCKED_HOSTS:
        raise UrlNotAllowed(f"禁止访问内部/元数据主机: {host}")
    # 字面量 IP
    try:
        ipaddress.ip_address(host)
        if _is_blocked_ip(host):
            raise UrlNotAllowed(f"禁止访问私网/环回/元数据地址: {host}")
    except UrlNotAllowed:
        raise
    except ValueError:
        pass
    # 域名：解析后校验所有解析到的 IP（防 DNS 指向内网）
    if resolve and not _looks_like_ip(host):
        try:
            infos = socket.getaddrinfo(host, None)
            for info in infos:
                ip = info[4][0]
                if _is_blocked_ip(ip):
                    raise UrlNotAllowed(f"域名 {host} 解析到受限地址 {ip}")
        except socket.gaierror:
            # 无法解析：放行到后续 HTTP（会自然失败），不在这里误判
            pass
    if not host_allowed(host, allowlist):
        raise UrlNotAllowed(f"主机 {host} 不在允许域名白名单内")


def _looks_like_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False
