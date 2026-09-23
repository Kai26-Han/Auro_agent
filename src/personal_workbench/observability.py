"""Durable, content-minimising traces for capability execution.

Traces contain topology, status, counts and identifiers.  Prompts, model
answers and tool payloads remain in their authoritative stores and are not
duplicated into observability records.
"""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


TRACE_SCHEMA_VERSION = 1
SPAN_TYPES = {
    "run", "capability", "attempt", "stage", "agent", "model",
    "retrieval", "tool", "approval", "artifact", "memory",
}
SPAN_STATUSES = {
    "pending", "running", "waiting_approval", "completed", "failed",
    "limited", "rejected", "conflict", "stopped", "interrupted",
    "skipped", "superseded", "unknown",
}
TERMINAL = SPAN_STATUSES - {"pending", "running", "waiting_approval"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def span_id(kind, *parts):
    """Stable bounded ID; raw provider/tool identifiers never become DB keys."""
    raw = ":".join(str(value) for value in parts)
    suffix = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return f"{kind}:{suffix}"


def root_span_id(trace_id): return span_id("run", trace_id)
def capability_span_id(trace_id): return span_id("capability", trace_id)
def attempt_span_id(job_id): return span_id("attempt", job_id)


class SpanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_schema_version: Literal[1] = TRACE_SCHEMA_VERSION
    trace_id: str = Field(min_length=1, max_length=128)
    span_id: str = Field(min_length=1, max_length=128)
    parent_span_id: str | None = Field(default=None, max_length=128)
    job_id: str | None = Field(default=None, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    thread_id: str = Field(min_length=1, max_length=64)
    capability_id: str = Field(min_length=1, max_length=128)
    capability_version: str = Field(min_length=1, max_length=128)
    span_type: Literal[
        "run", "capability", "attempt", "stage", "agent", "model",
        "retrieval", "tool", "approval", "artifact", "memory",
    ]
    name: str = Field(min_length=1, max_length=240)
    status: Literal[
        "pending", "running", "waiting_approval", "completed", "failed",
        "limited", "rejected", "conflict", "stopped", "interrupted",
        "skipped", "superseded", "unknown",
    ]
    attributes: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, int | bool | None] = Field(default_factory=dict)
    started_at: str | None = None
    ended_at: str | None = None


class TraceStore:
    def __init__(self, path):
        self.path = path
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS trace_spans(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT NOT NULL,
                    span_id TEXT NOT NULL,
                    parent_span_id TEXT,
                    job_id TEXT,
                    run_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    capability_id TEXT NOT NULL,
                    capability_version TEXT NOT NULL,
                    span_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attributes TEXT NOT NULL DEFAULT '{}',
                    usage TEXT NOT NULL DEFAULT '{}',
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    UNIQUE(trace_id, span_id)
                );
                CREATE INDEX IF NOT EXISTS trace_spans_trace ON trace_spans(trace_id, seq);
                CREATE INDEX IF NOT EXISTS trace_spans_job ON trace_spans(job_id, seq);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally:
            db.close()

    @staticmethod
    def _json(value):
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if len(encoded.encode()) > 20000:
            raise ValueError("Trace 元数据超过 20 KiB 限制。")
        return encoded

    def record(self, value):
        item = SpanUpdate.model_validate(value).model_dump()
        stamp = now()
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM trace_spans WHERE trace_id=? AND span_id=?",
                (item["trace_id"], item["span_id"]),
            ).fetchone()
            if row and any(row[key] != item[key] for key in
                           ("run_id","thread_id","capability_id","capability_version","span_type")):
                raise ValueError("Trace span 的固定身份字段发生变化。")
            attributes = {**(json.loads(row["attributes"]) if row else {}), **item["attributes"]}
            usage = {**(json.loads(row["usage"]) if row else {}), **item["usage"]}
            started = (row["started_at"] if row else None) or item.get("started_at") or stamp
            # A resumable logical span may return to running after interruption.
            open_status = item["status"] in {"pending", "running", "waiting_approval"}
            ended = None if open_status and item["span_type"] != "attempt" else (item.get("ended_at") or stamp)
            if row:
                db.execute('''UPDATE trace_spans SET parent_span_id=?,job_id=?,status=?,attributes=?,usage=?,started_at=?,ended_at=?
                              WHERE trace_id=? AND span_id=?''',
                           (item["parent_span_id"],item["job_id"] or row["job_id"],item["status"],
                            self._json(attributes),self._json(usage),started,ended,item["trace_id"],item["span_id"]))
            else:
                db.execute('''INSERT INTO trace_spans(trace_id,span_id,parent_span_id,job_id,run_id,thread_id,
                              capability_id,capability_version,span_type,name,status,attributes,usage,started_at,ended_at)
                              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                           (item["trace_id"],item["span_id"],item["parent_span_id"],item["job_id"],item["run_id"],
                            item["thread_id"],item["capability_id"],item["capability_version"],item["span_type"],item["name"],
                            item["status"],self._json(attributes),self._json(usage),started,ended))
        return item

    def begin_job(self, *, trace_id, job_id, run_id, thread_id, capability_id, capability_version, resumed):
        common = dict(trace_id=trace_id,run_id=run_id,thread_id=thread_id,
                      capability_id=capability_id,capability_version=capability_version)
        self.record({**common,"span_id":root_span_id(trace_id),"span_type":"run","name":"运行","status":"running",
                     "attributes":{"resumed":bool(resumed)}})
        self.record({**common,"span_id":capability_span_id(trace_id),"parent_span_id":root_span_id(trace_id),
                     "span_type":"capability","name":capability_id,"status":"running"})
        self.record({**common,"span_id":attempt_span_id(job_id),"parent_span_id":capability_span_id(trace_id),"job_id":job_id,
                     "span_type":"attempt","name":"恢复执行" if resumed else "首次执行","status":"running",
                     "attributes":{"resumed":bool(resumed)}})

    def finish_job(self, job, status):
        trace_id = job["trace_id"]
        common = dict(trace_id=trace_id,run_id=job["run_id"],thread_id=job["thread_id"],job_id=job["id"],
                      capability_id=job["capability_id"],capability_version=job["capability_version"])
        self.record({**common,"span_id":attempt_span_id(job["id"]),"parent_span_id":capability_span_id(trace_id),
                     "span_type":"attempt","name":"恢复执行" if job.get("resumed") else "首次执行","status":status})
        for sid,kind,name,parent in (
            (capability_span_id(trace_id),"capability",job["capability_id"],root_span_id(trace_id)),
            (root_span_id(trace_id),"run","运行",None),
        ):
            self.record({**common,"span_id":sid,"parent_span_id":parent,"span_type":kind,"name":name,"status":status})

    def trace(self, trace_id):
        with self.db() as db:
            rows = db.execute("SELECT * FROM trace_spans WHERE trace_id=? ORDER BY seq",(trace_id,)).fetchall()
        if not rows:
            raise ValueError("此任务还没有可观测记录。")
        spans=[]
        for row in rows:
            item=dict(row);item["attributes"]=json.loads(item["attributes"]);item["usage"]=json.loads(item["usage"])
            spans.append(item)
        counts={kind:sum(s["span_type"]==kind for s in spans) for kind in sorted(SPAN_TYPES)}
        model=[s for s in spans if s["span_type"]=="model"]
        usage={"model_calls":sum(int(s["usage"].get("model_calls") or 0) for s in model),
               "input_tokens":sum(int(s["usage"].get("input_tokens") or 0) for s in model),
               "output_tokens":sum(int(s["usage"].get("output_tokens") or 0) for s in model),
               "cached_input_tokens":sum(int(s["usage"].get("cached_input_tokens") or 0) for s in model),
               "cache_write_tokens":sum(int(s["usage"].get("cache_write_tokens") or 0) for s in model),
               "usage_tokens":sum(int(s["usage"].get("usage_tokens") or 0) for s in model),
               "usage_unknown":any(bool(s["usage"].get("usage_unknown")) for s in model)}
        return {"trace_schema_version":TRACE_SCHEMA_VERSION,"trace_id":trace_id,
                "root_span_id":root_span_id(trace_id),"spans":spans,"counts":counts,"usage":usage}
