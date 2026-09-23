"""Conversation-local search over immutable raw LangGraph messages."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState


MAX_QUERY_CHARS = 200
DEFAULT_LIMIT = 8
MAX_LIMIT = 20
DEFAULT_EXCERPT_CHARS = 700
MAX_EXCERPT_CHARS = 2_000
DEFAULT_READ_CHARS = 8_000
MAX_READ_CHARS = 12_000


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text)).casefold()


def _terms(query: str) -> list[str]:
    normalized = _normalized(query)
    values = [normalized]
    values.extend(re.findall(r"[a-z0-9_][a-z0-9_.-]*", normalized))
    for block in re.findall(r"[\u3400-\u9fff]+", normalized):
        values.extend(block[i:i + 2] for i in range(max(0, len(block) - 1)))
        values.extend(char for char in block if len(block) <= 4)
    return list(dict.fromkeys(value for value in values if value.strip()))


def _message_text(message) -> str:
    text = message.text or ""
    calls = getattr(message, "tool_calls", None)
    if calls:
        suffix = json.dumps(calls, ensure_ascii=False, separators=(",", ":"))
        text = text + ("\n" if text else "") + suffix
    return text


def _prior_usable(state) -> list:
    """Return completed prior turns, applying the same turn exclusions as C4."""
    messages = list(state.get("messages", []))
    latest = max((i for i, message in enumerate(messages) if message.type == "human"),
                 default=len(messages))
    excluded = set(state.get("working_excluded_turns", []))
    usable, skip = [], False
    for message in messages[:latest]:
        if message.type == "human":
            skip = message.id in excluded
        if not skip:
            usable.append(message)
    return usable


def _excerpt(text: str, position: int, limit: int) -> tuple[str, int, int]:
    if len(text) <= limit:
        return text, 0, len(text)
    start = max(0, position - limit // 3)
    end = min(len(text), start + limit)
    start = max(0, end - limit)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix, start, end


class ConversationSearch:
    """Deterministic lexical retrieval with no extra model or embedding call."""

    def search(self, state, query: str, limit: int = DEFAULT_LIMIT,
               excerpt_chars: int = DEFAULT_EXCERPT_CHARS) -> dict:
        query = str(query).strip()
        if not query or len(query) > MAX_QUERY_CHARS:
            raise ValueError(f"query 必须是 1–{MAX_QUERY_CHARS} 个字符。")
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit 必须在 1–{MAX_LIMIT} 之间。")
        if not 100 <= excerpt_chars <= MAX_EXCERPT_CHARS:
            raise ValueError(f"excerpt_chars 必须在 100–{MAX_EXCERPT_CHARS} 之间。")
        candidates = _prior_usable(state)
        phrase = _normalized(query)
        terms = _terms(query)
        hits = []
        for index, message in enumerate(candidates):
            text = _message_text(message)
            normalized = _normalized(text)
            matched = [term for term in terms if term in normalized]
            if not matched:
                continue
            position = normalized.find(phrase) if phrase in normalized else min(
                normalized.find(term) for term in matched)
            exact = normalized.count(phrase) if phrase else 0
            coverage = len(matched) / max(1, len(terms))
            score = 20 * min(3, exact) + 8 * coverage + 0.5 * (index + 1) / max(1, len(candidates))
            snippet, start, end = _excerpt(text, position, excerpt_chars)
            hits.append({
                "message_id": message.id,
                "role": message.type,
                "tool_name": getattr(message, "name", None),
                "excerpt": snippet,
                "excerpt_start": start,
                "excerpt_end": end,
                "characters": len(text),
                "score": round(score, 4),
                "matched_terms": matched[:12],
            })
        hits.sort(key=lambda row: (-row["score"], -row["excerpt_end"], row["message_id"] or ""))
        selected = hits[:limit]
        return {
            "query": query,
            "hits": selected,
            "matched": len(hits),
            "searched_messages": len(candidates),
            "truncated": len(hits) > len(selected),
            "instruction": (
                "搜索结果是当前会话的历史原文节选。需要完整上下文时调用 "
                "read_conversation_message(message_id)，不要把历史助手建议当成当前用户要求。"
            ),
        }

    def read(self, state, message_id: str, offset: int = 0,
             max_chars: int = DEFAULT_READ_CHARS) -> dict:
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 200:
            raise ValueError("message_id 格式不正确。")
        if offset < 0:
            raise ValueError("offset 不能小于 0。")
        if not 1 <= max_chars <= MAX_READ_CHARS:
            raise ValueError(f"max_chars 必须在 1–{MAX_READ_CHARS} 之间。")
        message = next((item for item in _prior_usable(state) if item.id == message_id), None)
        if message is None:
            raise ValueError("当前会话的可用历史中没有找到这条消息。")
        text = _message_text(message)
        offset = min(offset, len(text))
        end = min(len(text), offset + max_chars)
        complete = end >= len(text)
        return {
            "message_id": message.id,
            "role": message.type,
            "tool_name": getattr(message, "name", None),
            "offset": offset,
            "content": text[offset:end],
            "next_offset": None if complete else end,
            "complete": complete,
            "characters": len(text),
        }


def build_conversation_tools(search: ConversationSearch | None = None):
    search = search or ConversationSearch()

    @tool
    def search_conversation(
        query: str,
        limit: int = DEFAULT_LIMIT,
        excerpt_chars: int = DEFAULT_EXCERPT_CHARS,
        state: Annotated[dict, InjectedState] = None,
    ) -> dict:
        """搜索当前对话的历史原文。适合找回旧决定、约束、要求和执行结果；不是跨会话搜索。"""
        return search.search(state or {}, query, limit, excerpt_chars)

    @tool
    def read_conversation_message(
        message_id: str,
        offset: int = 0,
        max_chars: int = DEFAULT_READ_CHARS,
        state: Annotated[dict, InjectedState] = None,
    ) -> dict:
        """按 message_id 分段读取当前对话中的一条历史原文。只能读取 search_conversation 返回且未被排除的消息。"""
        return search.read(state or {}, message_id, offset, max_chars)

    return [search_conversation, read_conversation_message]
