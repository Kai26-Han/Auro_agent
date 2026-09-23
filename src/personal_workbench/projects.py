"""Local project containers and conversation lifecycle metadata."""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProjectStore:
    """Own project records and the user-facing lifecycle of conversations."""

    SESSION_COLUMNS = {
        "project_id": "TEXT",
        "pinned_at": "TEXT",
        "archived_at": "TEXT",
        "title_manual": "INTEGER NOT NULL DEFAULT 0",
    }

    def __init__(self, settings):
        self.settings = settings
        self.path = settings.data_dir / "app.sqlite"
        self.initialize()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created TEXT NOT NULL,
                    updated TEXT NOT NULL,
                    pinned_at TEXT,
                    archived_at TEXT
                );
                CREATE INDEX IF NOT EXISTS projects_active_order
                ON projects(archived_at, sort_order, updated);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
            for name, kind in self.SESSION_COLUMNS.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {kind}")
            db.execute("CREATE INDEX IF NOT EXISTS sessions_project ON sessions(project_id, archived_at, updated)")
            db.execute("CREATE INDEX IF NOT EXISTS sessions_pinned ON sessions(pinned_at, archived_at)")
            project_columns = {row[1] for row in db.execute("PRAGMA table_info(projects)")}
            if "pinned_at" not in project_columns:
                db.execute("ALTER TABLE projects ADD COLUMN pinned_at TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS projects_pinned_order ON projects(archived_at, pinned_at, updated)")

    @staticmethod
    def _public(row):
        value = dict(row)
        return {
            "id": value["id"], "name": value["name"],
            "sort_order": value.get("sort_order", 0),
            "conversation_count": value.get("conversation_count", 0),
            "pinned": bool(value.get("pinned_at")), "pinned_at": value.get("pinned_at"),
            "archived": bool(value.get("archived_at")), "archived_at": value.get("archived_at"),
            "created": value["created"], "updated": value["updated"],
        }

    def list(self, archived=False):
        with self.connect() as db:
            rows = db.execute(
                """SELECT p.id,p.name,p.sort_order,p.created,p.updated,p.pinned_at,p.archived_at,
                    (SELECT COUNT(*) FROM sessions s WHERE s.project_id=p.id AND s.archived_at IS NULL) AS conversation_count
                   FROM projects p WHERE archived_at IS %s
                   ORDER BY %s""" % (
                       "NOT NULL" if archived else "NULL",
                       "updated DESC, name" if archived else "pinned_at IS NULL, pinned_at DESC, sort_order, updated DESC, name",
                   )
            ).fetchall()
        return [self._public(row) for row in rows]

    def get(self, pid, include_archived=False):
        with self.connect() as db:
            row = db.execute("""SELECT p.id,p.name,p.sort_order,p.created,p.updated,p.pinned_at,p.archived_at,
                (SELECT COUNT(*) FROM sessions s WHERE s.project_id=p.id AND s.archived_at IS NULL) AS conversation_count
                FROM projects p WHERE p.id=?""", (pid,)).fetchone()
        if row is None or (row["archived_at"] and not include_archived):
            raise ValueError("项目不存在或已归档。")
        return self._public(row)

    def create(self, value):
        stamp, pid = _now(), uuid4().hex
        name = value["name"].strip()
        with self.connect() as db:
            if db.execute("SELECT COUNT(*) FROM projects WHERE archived_at IS NULL").fetchone()[0] >= 50:
                raise ValueError("最多可以建立 50 个未归档项目。")
            db.execute("INSERT INTO projects(id,name,sort_order,created,updated,pinned_at,archived_at) VALUES (?,?,0,?,?,NULL,NULL)",
                       (pid, name, stamp, stamp))
        return self.get(pid)

    def update(self, pid, patch):
        current = self.get(pid, include_archived=True)
        name = str(patch.get("name", current["name"])).strip()
        pinned_at = current.get("pinned_at")
        if "pinned" in patch:
            pinned_at = _now() if patch["pinned"] else None
        with self.connect() as db:
            db.execute("UPDATE projects SET name=?,pinned_at=?,updated=? WHERE id=?",
                       (name, pinned_at, _now(), pid))
        return self.get(pid, include_archived=True)

    def archive(self, pid, archived=True):
        self.get(pid, include_archived=True)
        stamp = _now()
        with self.connect() as db:
            db.execute("UPDATE projects SET archived_at=?,pinned_at=?,updated=? WHERE id=?",
                       (stamp if archived else None, None, stamp, pid))
        return self.get(pid, include_archived=True)

    def delete(self, pid):
        self.get(pid, include_archived=True)
        with self.connect() as db:
            db.execute("UPDATE sessions SET project_id=NULL WHERE project_id=?", (pid,))
            db.execute("DELETE FROM projects WHERE id=?", (pid,))
        return {"deleted": True, "conversations_preserved": True}

    def update_conversation(self, tid, patch):
        allowed = {"title", "project_id", "pinned"}
        if set(patch) - allowed:
            raise ValueError("会话更新包含不支持的字段。")
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (tid,)).fetchone()
            if row is None:
                raise ValueError("没有找到此会话。")
            if "title" in patch:
                title = str(patch["title"]).strip()
                if not 1 <= len(title) <= 120:
                    raise ValueError("会话名称必须为 1–120 个字符。")
                db.execute("UPDATE sessions SET title=?,title_manual=1 WHERE id=?", (title, tid))
            if "project_id" in patch:
                pid = patch["project_id"] or None
                if pid and not db.execute("SELECT 1 FROM projects WHERE id=? AND archived_at IS NULL", (pid,)).fetchone():
                    raise ValueError("目标项目不存在或已归档。")
                db.execute("UPDATE sessions SET project_id=? WHERE id=?", (pid, tid))
            if "pinned" in patch:
                db.execute("UPDATE sessions SET pinned_at=? WHERE id=?", (_now() if patch["pinned"] else None, tid))
            db.execute("UPDATE sessions SET updated=? WHERE id=?", (_now(), tid))
            result = db.execute("SELECT * FROM sessions WHERE id=?", (tid,)).fetchone()
        return self.session_public(result)

    def archive_conversation(self, tid, archived=True):
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (tid,)).fetchone()
            if row is None:
                raise ValueError("没有找到此会话。")
            db.execute("UPDATE sessions SET archived_at=?,pinned_at=NULL WHERE id=?",
                       (_now() if archived else None, tid))
            result = db.execute("SELECT * FROM sessions WHERE id=?", (tid,)).fetchone()
        return self.session_public(result)

    @staticmethod
    def session_public(row):
        value = dict(row)
        return {
            "id": value["id"], "title": value["title"], "status": value["status"],
            "mode": value["mode"], "updated": value["updated"],
            "project_id": value.get("project_id"), "pinned": bool(value.get("pinned_at")),
            "pinned_at": value.get("pinned_at"), "archived": bool(value.get("archived_at")),
            "archived_at": value.get("archived_at"),
        }

    def enrich_sessions(self, sessions, *, archived=False, project_id=None, unassigned=False,
                        pinned=None, query="", offset=0, limit=30):
        by_id = {item["id"]: dict(item) for item in sessions}
        with self.connect() as db:
            metadata = {row["id"]: dict(row) for row in db.execute(
                "SELECT id,project_id,pinned_at,archived_at,title,title_manual FROM sessions"
            )}
        values = []
        needle = query.strip().casefold()
        for sid, item in by_id.items():
            meta = metadata.get(sid, {})
            item.update(project_id=meta.get("project_id"), pinned=bool(meta.get("pinned_at")),
                        pinned_at=meta.get("pinned_at"), archived=bool(meta.get("archived_at")),
                        archived_at=meta.get("archived_at"))
            if item["archived"] != archived:
                continue
            if project_id is not None and item.get("project_id") != project_id:
                continue
            if unassigned and item.get("project_id") is not None:
                continue
            if pinned is not None and item["pinned"] != pinned:
                continue
            if needle and needle not in item.get("title", "").casefold():
                continue
            values.append(item)
        values.sort(key=lambda item: (item.get("pinned_at") or "", item.get("updated") or ""), reverse=True)
        total = len(values)
        return {"items": values[offset:offset + limit], "total": total,
                "offset": offset, "limit": limit, "has_more": offset + limit < total}


class ConversationDeletionService:
    """Idempotently remove conversation-owned state while preserving notes and long-term memory."""

    def __init__(self, settings, projects: ProjectStore, jobs, attachments=None):
        self.settings, self.projects, self.jobs = settings, projects, jobs
        self.attachments = attachments

    @staticmethod
    def _delete_where(path: Path, tables: dict[str, str], tid: str):
        if not path.exists():
            return
        with sqlite3.connect(path, timeout=20) as db:
            existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, column in tables.items():
                if table in existing:
                    columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
                    if column in columns:
                        db.execute(f'DELETE FROM "{table}" WHERE "{column}"=?', (tid,))

    def delete(self, tid, delete_artifacts=False):
        if self.jobs.active_for_thread(tid):
            raise ValueError("当前会话仍在运行，请先停止并等待结束。")
        with self.projects.connect() as db:
            if not db.execute("SELECT 1 FROM sessions WHERE id=?", (tid,)).fetchone():
                raise ValueError("没有找到此会话。")
        folder = self.settings.outputs_dir / tid
        if delete_artifacts and folder.exists() and folder.is_symlink():
            raise ValueError("成果目录是符号链接，已拒绝删除。")

        # Checkpoint history is the actual conversation transcript.
        self._delete_where(self.settings.data_dir / "checkpoints.sqlite", {
            "checkpoints": "thread_id", "writes": "thread_id", "checkpoint_writes": "thread_id",
            "blobs": "thread_id", "checkpoint_blobs": "thread_id",
        }, tid)
        # Job/event rows and traces are owned by the conversation.
        web = self.settings.data_dir / "web.sqlite"
        if web.exists():
            with sqlite3.connect(web, timeout=20) as db:
                job_ids = [row[0] for row in db.execute("SELECT id FROM jobs WHERE thread_id=?", (tid,))]
                if job_ids:
                    marks = ",".join("?" for _ in job_ids)
                    db.execute(f"DELETE FROM events WHERE job_id IN ({marks})", job_ids)
                for table, column in (("trace_spans", "thread_id"), ("turn_revisions", "thread_id"),
                                      ("bindings", "thread_id"), ("jobs", "thread_id")):
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                        db.execute(f'DELETE FROM "{table}" WHERE "{column}"=?', (tid,))

        # Remove only short-term/context records. Long-term memory items and their
        # provenance remain available by design.
        self._delete_where(self.settings.data_dir / "app.sqlite", {
            "tool_results": "thread_id", "sessions": "id",
        }, tid)
        memory_profiles = self.settings.data_dir / "memory-profiles"
        self._delete_where(memory_profiles / "mem0" / "mem0-default" / "native" / "management.sqlite", {
            "contexts": "thread_id", "context_exclusions": "thread_id", "context_barriers": "thread_id",
        }, tid)
        for path in (memory_profiles / "langmem" / "langmem-default" / "store.sqlite",
                     memory_profiles / "mem0" / "mem0-default" / "store.sqlite"):
            self._delete_where(path, {"lifecycle_contexts": "thread_id", "episode_tracks": "thread_id"}, tid)

        if self.attachments:
            self.attachments.delete_thread(tid)

        if delete_artifacts:
            if folder.exists():
                shutil.rmtree(folder)
        return {"deleted": True, "artifacts_deleted": bool(delete_artifacts),
                "notes_preserved": True, "long_term_memory_preserved": True}
