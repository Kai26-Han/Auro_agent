"""Durable, conversation-scoped storage for large tool results.

Tool calls must still receive exactly one ToolMessage, but that message does
not need to contain the entire payload.  C3 stores large successful payloads
in SQLite and replaces the message with a bounded preview and a stable id.
The model can then read the original result in explicit chunks.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool


INLINE_RESULT_BYTES = 12_000
PREVIEW_CHARS = 3_000
DEFAULT_READ_CHARS = 8_000
MAX_READ_CHARS = 12_000
RESULT_ID = re.compile(r"tr_[0-9a-f]{32}")

# These fields let the model understand what the result contains without
# copying document/page bodies into every later prompt.
SAFE_REFERENCE_FIELDS = (
    "citation", "chunk_id", "source_id", "path", "url", "title",
    "kb_id", "kb_name", "page", "start", "end", "line", "sha256",
    "provider", "kind", "score", "external", "engine",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, separators=(",", ":"))


def _bounded_references(value, limit=20):
    if not isinstance(value, list):
        return []
    rows = []
    for item in value[:limit]:
        if isinstance(item, dict):
            row = {}
            for key in SAFE_REFERENCE_FIELDS:
                if key not in item:
                    continue
                field = item[key]
                row[key] = field[:500] if isinstance(field, str) else field
            rows.append(row)
    return rows


class ToolResultStore:
    """Store complete tool payloads in the assistant's durable SQLite DB."""

    def __init__(self, db):
        self.db = db
        database = self.db.execute("PRAGMA database_list").fetchone()
        self.path = str(database[2]) if database and database[2] else ""
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS tool_results (
                id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                content TEXT NOT NULL,
                bytes INTEGER NOT NULL,
                characters INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                created TEXT NOT NULL,
                UNIQUE(thread_id, turn_id, tool_call_id, sha256)
            )
        """)
        self.db.execute("CREATE INDEX IF NOT EXISTS tool_results_thread ON tool_results(thread_id, created)")
        self.db.commit()

    @staticmethod
    def result_id(thread_id: str, turn_id: str, tool_call_id: str, tool_name: str, content: str) -> str:
        digest = hashlib.sha256(
            "\0".join((thread_id, turn_id, tool_call_id, tool_name, content)).encode("utf-8")
        ).hexdigest()
        return "tr_" + digest[:32]

    def put(self, thread_id: str, turn_id: str, tool_call_id: str, tool_name: str, content) -> dict:
        content = _text(content)
        raw = content.encode("utf-8")
        sha = hashlib.sha256(raw).hexdigest()
        rid = self.result_id(thread_id, turn_id, tool_call_id, tool_name, content)
        self.db.execute(
            "INSERT OR IGNORE INTO tool_results VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rid, thread_id, turn_id, tool_call_id, tool_name, content,
             len(raw), len(content), sha, _now()),
        )
        self.db.commit()
        return {
            "result_id": rid, "tool_name": tool_name, "bytes": len(raw),
            "characters": len(content), "sha256": sha,
        }

    def read(self, thread_id: str, result_id: str, offset: int = 0,
             max_chars: int = DEFAULT_READ_CHARS) -> dict:
        if not RESULT_ID.fullmatch(str(result_id)):
            raise ValueError("结果 ID 格式不正确。")
        if offset < 0:
            raise ValueError("offset 不能小于 0。")
        if not 1 <= max_chars <= MAX_READ_CHARS:
            raise ValueError(f"max_chars 必须在 1–{MAX_READ_CHARS} 之间。")
        # LangGraph may execute sync tools in a worker thread. A fresh
        # connection used only for this query avoids sharing the service
        # connection across threads; in-memory stores remain useful for tests.
        reader = sqlite3.connect(self.path) if self.path else self.db
        if reader is not self.db:
            reader.row_factory = sqlite3.Row
        try:
            row = reader.execute(
                "SELECT * FROM tool_results WHERE id=? AND thread_id=?",
                (result_id, thread_id),
            ).fetchone()
        finally:
            if reader is not self.db:
                reader.close()
        if row is None:
            # Deliberately do not reveal whether another conversation owns it.
            raise ValueError("当前会话中没有找到这个工具结果。")
        content = row["content"]
        offset = min(offset, len(content))
        end = min(len(content), offset + max_chars)
        complete = end >= len(content)
        return {
            "result_id": row["id"], "tool_name": row["tool_name"],
            "offset": offset, "content": content[offset:end],
            "next_offset": None if complete else end, "complete": complete,
            "characters": row["characters"], "bytes": row["bytes"],
            "sha256": row["sha256"],
        }

    def list(self, thread_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT id,tool_name,bytes,characters,sha256,created FROM tool_results "
            "WHERE thread_id=? ORDER BY created,id", (thread_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def compact(self, response: ToolMessage, *, thread_id: str, turn_id: str,
                tool_call_id: str, tool_name: str) -> tuple[ToolMessage, dict | None]:
        """Return a protocol-compatible ToolMessage and optional result ref."""
        content = _text(response.content)
        if (response.status == "error" or tool_name == "read_tool_result"
                or len(content.encode("utf-8")) <= INLINE_RESULT_BYTES):
            return response, None

        ref = self.put(thread_id, turn_id, tool_call_id, tool_name, content)
        try:
            parsed = json.loads(content)
        except (ValueError, TypeError):
            parsed = None
        metadata = {}
        if isinstance(parsed, dict):
            metadata = {
                "keys": list(parsed)[:30],
                "sources": _bounded_references(parsed.get("sources")),
                "hits": _bounded_references(parsed.get("hits")),
            }
            for key in ("total", "count", "truncated", "next_offset", "provider", "query"):
                if key in parsed and isinstance(parsed[key], (str, int, float, bool, type(None))):
                    metadata[key] = parsed[key]
            receipt = parsed.get("operation_receipt")
            if isinstance(receipt, dict):
                # This is host-generated bounded state, not untrusted stdout.
                metadata["operation_receipt"] = receipt
        preview = content[:PREVIEW_CHARS]
        payload = {
            **ref,
            "stored": True,
            "preview": preview,
            "preview_characters": len(preview),
            "metadata": metadata,
            "instruction": (
                "这是完整结果的预览。需要核对、引用或继续分析时，调用 "
                "read_tool_result，并传入 result_id；从 offset=0 开始，按 next_offset 继续读取。"
            ),
        }
        # Metadata originates in tools/connectors and can itself contain long
        # titles or URLs. Keep the replacement strictly smaller than the
        # externalization threshold even for unusual payload shapes.
        while len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 10_000:
            if len(payload["preview"]) > 800:
                payload["preview"] = payload["preview"][:len(payload["preview"]) // 2]
                payload["preview_characters"] = len(payload["preview"])
                continue
            reduced = False
            for key in ("hits", "sources", "keys"):
                values = payload["metadata"].get(key)
                if isinstance(values, list) and len(values) > 1:
                    payload["metadata"][key] = values[:max(1, len(values) // 2)]
                    reduced = True
            if not reduced:
                payload["metadata"] = {"keys": payload["metadata"].get("keys", [])[:5]}
                break
        compacted = response.model_copy(update={
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        })
        return compacted, ref


def build_tool_result_tool(store: ToolResultStore, thread_id: str):
    @tool
    def read_tool_result(result_id: str, offset: int = 0,
                         max_chars: int = DEFAULT_READ_CHARS) -> dict:
        """渐进读取当前会话中已保存的大型工具结果。先用 offset=0；若 complete=false，再使用返回的 next_offset 继续。"""
        return store.read(thread_id, result_id, offset, max_chars)

    return read_tool_result
