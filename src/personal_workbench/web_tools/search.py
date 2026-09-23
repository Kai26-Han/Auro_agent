"""网页搜索供应商和 arXiv 元数据检索适配层。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import threading
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import httpx

MAX_BYTES = 4 * 1024 * 1024
_arxiv_lock = threading.Lock()
_arxiv_last = 0.0


def request_bytes(method: str, url: str, *, timeout: int, **kwargs) -> bytes:
    """执行一次受超时和响应大小限制的 HTTP 请求。"""

    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        with client.stream(method, url, **kwargs) as response:
            response.raise_for_status()
            if response.is_redirect:
                raise ValueError("服务返回重定向，请检查配置。")
            payload = bytearray()
            deadline = time.monotonic() + timeout
            for chunk in response.iter_bytes(chunk_size=65_536):
                if time.monotonic() > deadline:
                    raise ValueError("服务响应超时。")
                if len(payload) + len(chunk) > MAX_BYTES:
                    raise ValueError("服务返回内容超过 4 MiB。")
                payload.extend(chunk)
            return bytes(payload)


def public_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password


def web_search(query, config, max_results=5):
    count = min(max_results, config.max_results)
    provider = config.provider
    if provider in {"bing", "duckduckgo"}:
        from ddgs import DDGS

        rows = list(DDGS(timeout=config.timeout).text(query, max_results=count, backend=provider))
        return _normalise_web_rows(rows, provider, count, "href", "body")

    if provider in {"tavily", "brave"} and not config.api_key:
        raise ValueError("请先在设置中填写搜索服务的 API Key。")
    if provider == "tavily":
        payload = request_bytes(
            "POST",
            "https://api.tavily.com/search",
            timeout=config.timeout,
            json={
                "api_key": config.api_key,
                "query": query,
                "max_results": count,
                "search_depth": "basic",
                "include_answer": False,
                "include_raw_content": False,
            },
        )
        rows = json.loads(payload).get("results", [])
        return _normalise_web_rows(rows, provider, count, "url", "content")
    if provider == "brave":
        payload = request_bytes(
            "GET",
            "https://api.search.brave.com/res/v1/web/search",
            timeout=config.timeout,
            headers={"Accept": "application/json", "X-Subscription-Token": config.api_key},
            params={"q": query, "count": count},
        )
        rows = json.loads(payload).get("web", {}).get("results", [])
        return _normalise_web_rows(rows, provider, count, "url", "description")

    payload = request_bytes(
        "GET",
        f"{config.base_url}/search",
        timeout=config.timeout,
        params={"q": query, "format": "json"},
    )
    rows = json.loads(payload).get("results", [])
    return _normalise_web_rows(rows, provider, count, "url", "content")


def _normalise_web_rows(rows, provider: str, count: int, url_key: str, text_key: str) -> list[dict]:
    results = []
    for row in rows:
        url = str(row.get(url_key, ""))[:2048]
        if not public_url(url):
            continue
        results.append(
            {
                "title": str(row.get("title", ""))[:300],
                "url": url,
                "text": str(row.get(text_key, ""))[:3000],
                "provider": provider,
                "kind": "web_search",
            }
        )
        if len(results) == count:
            break
    return results


def paper_search(query, max_results=3, years_limit=3, sort_by="relevance", timeout=20):
    """通过 arXiv Atom API 检索论文元数据和摘要。"""

    global _arxiv_last
    with _arxiv_lock:
        wait_seconds = 3 - (time.monotonic() - _arxiv_last)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        try:
            payload = request_bytes(
                "GET",
                "https://export.arxiv.org/api/query",
                timeout=timeout,
                headers={"User-Agent": "Auro/0.1 (personal research assistant)"},
                params={
                    "search_query": query,
                    "max_results": min(max_results * 2, 30),
                    "sortBy": "submittedDate" if sort_by == "date" else "relevance",
                    "sortOrder": "descending",
                },
            )
        finally:
            _arxiv_last = time.monotonic()

    root = ET.fromstring(payload)
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    current_year = datetime.now(timezone.utc).year
    results = []
    for entry in root.findall("atom:entry", namespace):
        read = lambda name: " ".join(entry.findtext(f"atom:{name}", "", namespace).split())
        identifier = read("id")
        if identifier.endswith("/api/errors"):
            raise ValueError("arXiv 查询语法无效，请缩短或修改关键词。")
        published = read("published")
        if not published:
            continue
        year = int(published[:4])
        if years_limit and current_year - year > years_limit:
            continue
        url = identifier.replace("http://", "https://", 1)
        if not public_url(url):
            continue
        abstract = read("summary")[:6000]
        authors = [node.findtext("atom:name", "", namespace) for node in entry.findall("atom:author", namespace)]
        results.append(
            {
                "title": read("title")[:300],
                "authors": authors,
                "year": year,
                "published": published,
                "abstract": abstract,
                "text": abstract,
                "url": url,
                "arxiv_id": url.rsplit("/", 1)[-1].split("v")[0],
                "kind": "paper_search",
                "provider": "arxiv",
            }
        )
        if len(results) == max_results:
            break
    return results
