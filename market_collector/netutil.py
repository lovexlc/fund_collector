"""网络出口助手：数据源直连优先，失败时经机器级出口代理重试。

背景：东财部分 API（如 push2his K 线）对数据中心 IP 段有 WAF 拦截（TCP 可达但请求被
RST）；宿主机自带的 ga.dp.tech:8118 出口代理（登录 shell 的 http_proxy 指向它）可正常
访问。此代理是 Bohrium 平台的 egress，不是 CF workers。

策略：
- 一律先直连；
- 仅当目标域在 PROXY_FALLBACK_HOSTS 内（东财/蛋卷）且直连抛错时，经出口代理重试一次；
- 其余域（腾讯/新浪/SEC 等）直连失败直接抛错，绝不引入新依赖。
"""
from __future__ import annotations

import os
import urllib.request
from typing import Any

DEFAULT_EGRESS_PROXY = "http://ga.dp.tech:8118"
PROXY_FALLBACK_HOSTS = ("eastmoney.com", "danjuanfunds.com")

DEFAULT_HEADERS = {
    "accept": "application/json",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
}


def egress_proxy() -> str:
    return os.environ.get("MARKET_COLLECTOR_EGRESS_PROXY") or DEFAULT_EGRESS_PROXY


def proxy_fallback_enabled(url: str) -> bool:
    lowered = str(url or "").lower()
    return any(host in lowered for host in PROXY_FALLBACK_HOSTS)


def _open_direct(request: urllib.request.Request, timeout_sec: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout_sec) as response:
        return response.read()


def _open_via_proxy(request: urllib.request.Request, timeout_sec: float) -> bytes:
    proxy_url = egress_proxy()
    if not proxy_url:
        raise OSError("egress proxy disabled")
    handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    opener = urllib.request.build_opener(handler)
    with opener.open(request, timeout=timeout_sec) as response:
        return response.read()


def fetch_url(
    url: str,
    timeout_sec: float,
    headers: dict[str, str] | None = None,
) -> bytes:
    request = urllib.request.Request(url, headers={**DEFAULT_HEADERS, **(headers or {})})
    try:
        return _open_direct(request, timeout_sec)
    except Exception:
        if not proxy_fallback_enabled(url):
            raise
    return _open_via_proxy(request, timeout_sec)
