"""安全读取公开网页并提取适合模型使用的正文。"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from lxml import html

DEFAULT_MAX_CHARS = 50_000
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_USER_AGENT = "Auro/0.1"
ALLOWED_SCHEMES = {"http", "https"}
REDIRECT_CODES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 5


@dataclass(frozen=True)
class FetchOutcome:
    """网页读取结果；失败时通过 ``error`` 返回可展示的原因。"""

    ok: bool
    markdown: str = ""
    url: str = ""
    title: str = ""
    truncated: bool = False
    error: str = ""


async def fetch_url_as_markdown(
    url: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    user_agent: str = DEFAULT_USER_AGENT,
    client_factory: Any = None,
    host_validator: Any = None,
) -> FetchOutcome:
    """读取公开 HTTP(S) 页面，并把正文整理为简洁 Markdown。"""

    current_url = (url or "").strip().strip("`\"'")
    validator = host_validator or _is_disallowed_host
    invalid = _validate_target(current_url, validator)
    if invalid:
        return FetchOutcome(ok=False, error=invalid)

    factory = client_factory or _default_client_factory
    try:
        async with factory(timeout=timeout_s, user_agent=user_agent) as client:
            for redirect_count in range(MAX_REDIRECTS + 1):
                invalid = _validate_target(current_url, validator)
                if invalid:
                    prefix = "Redirect target blocked" if redirect_count else "URL blocked"
                    return FetchOutcome(ok=False, error=f"{prefix}: {invalid}")

                async with client.stream(
                    "GET",
                    current_url,
                    headers={"User-Agent": user_agent, "Accept": "text/html,text/plain,application/json,application/xml;q=0.8,*/*;q=0.2"},
                    follow_redirects=False,
                ) as response:
                    location = response.headers.get("location", "")
                    if response.status_code in REDIRECT_CODES and location:
                        if redirect_count == MAX_REDIRECTS:
                            return FetchOutcome(ok=False, error="Too many HTTP redirects.")
                        current_url = urljoin(current_url, location)
                        continue

                    final_url = str(response.url) or current_url
                    if response.status_code >= 400:
                        return FetchOutcome(ok=False, url=final_url, error=f"HTTP {response.status_code} from {final_url}.")
                    media_type = response.headers.get("content-type", "").lower()
                    if media_type and not any(kind in media_type for kind in ("text/", "json", "xml", "xhtml")):
                        return FetchOutcome(ok=False, url=final_url, error="Only text webpages are supported.")
                    payload = await _bounded_read(response, MAX_RESPONSE_BYTES)
                    break
            else:  # pragma: no cover
                return FetchOutcome(ok=False, error="Too many HTTP redirects.")
    except ValueError as exc:
        return FetchOutcome(ok=False, error=str(exc))
    except httpx.HTTPError as exc:
        return FetchOutcome(ok=False, error=f"Network error: {exc}")
    except Exception as exc:  # 保持工具返回结构稳定，避免任务线程崩溃
        return FetchOutcome(ok=False, error=f"Unexpected fetch failure: {exc}")

    title, body = _extract_readable(payload, base_url=final_url)
    truncated = len(body) > max_chars
    if truncated:
        body = body[:max_chars].rstrip() + "\n…[truncated]"
    return FetchOutcome(ok=True, markdown=body, url=final_url, title=title, truncated=truncated)


def _default_client_factory(*, timeout: float, user_agent: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, headers={"User-Agent": user_agent}, follow_redirects=False)


def _validate_target(url: str, validator: Any) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "Invalid URL."
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return f"Unsupported URL scheme: {parsed.scheme or '(empty)'}. Use http:// or https://."
    if not parsed.hostname:
        return "URL is missing a host."
    if parsed.username or parsed.password:
        return "Credentials in URLs are not supported."
    if validator(parsed.hostname):
        return f"Private or local host is not allowed: {parsed.hostname}."
    return ""


def _is_disallowed_host(host: str) -> bool:
    """仅允许所有 DNS 解析结果都属于公网地址的主机。"""

    candidate = host.strip("[]")
    try:
        return _is_disallowed_ip(ipaddress.ip_address(candidate))
    except ValueError:
        pass
    lower = candidate.lower()
    if lower in {"localhost", "ip6-localhost", "ip6-loopback"} or lower.endswith(".local"):
        return True
    try:
        answers = socket.getaddrinfo(candidate, None)
    except OSError:
        return True
    if not answers:
        return True
    for answer in answers:
        try:
            address = ipaddress.ip_address(answer[4][0])
        except (IndexError, TypeError, ValueError):
            return True
        if _is_disallowed_ip(address):
            return True
    return False


def _is_disallowed_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


async def _bounded_read(response: httpx.Response, limit: int) -> str:
    content = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=65_536):
        if len(content) + len(chunk) > limit:
            raise ValueError("Response exceeds 4 MiB limit.")
        content.extend(chunk)
    encoding = response.encoding or "utf-8"
    try:
        return content.decode(encoding, errors="replace")
    except (LookupError, TypeError):
        return content.decode("utf-8", errors="replace")


def _extract_readable(html_or_text: str, base_url: str = "") -> tuple[str, str]:
    """从 HTML 中选择正文区域并转成轻量 Markdown。"""

    if "<" not in html_or_text or ">" not in html_or_text:
        return "", _clean_text(html_or_text)
    try:
        document = html.fromstring(html_or_text, base_url=base_url or None)
    except (ValueError, TypeError):
        return "", _clean_text(re.sub(r"<[^>]+>", " ", html_or_text))

    for node in document.xpath("//script|//style|//nav|//header|//footer|//aside|//form|//noscript|//template|//svg"):
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)

    title_nodes = document.xpath("//title")
    title = _inline_text(title_nodes[0]) if title_nodes else ""
    candidates = document.xpath("//main|//article|//*[@role='main']")
    root = max(candidates, key=lambda node: len(_inline_text(node)), default=document)
    blocks = _markdown_blocks(root)
    body = "\n\n".join(blocks).strip() or _clean_text(root.text_content())
    if title and not body.lstrip().startswith(f"# {title}"):
        body = f"# {title}\n\n{body}" if body else f"# {title}"
    return title, body


def _markdown_blocks(root: Any) -> list[str]:
    blocks: list[str] = []
    block_tags = {"p", "li", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6"}
    nodes = [root, *root.iterdescendants()]
    for node in nodes:
        tag = str(getattr(node, "tag", "")).lower()
        if tag not in block_tags:
            continue
        text = _inline_text(node)
        if not text:
            continue
        if tag.startswith("h"):
            value = f"{'#' * int(tag[1])} {text}"
        elif tag == "li":
            value = f"- {text}"
        elif tag == "blockquote":
            value = f"> {text}"
        elif tag == "pre":
            value = f"```\n{text}\n```"
        else:
            value = text
        if not blocks or blocks[-1] != value:
            blocks.append(value)
    return blocks


def _inline_text(node: Any) -> str:
    return _clean_text(" ".join(node.itertext()))


def _clean_text(value: str) -> str:
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


__all__ = ["DEFAULT_MAX_CHARS", "FetchOutcome", "fetch_url_as_markdown"]
