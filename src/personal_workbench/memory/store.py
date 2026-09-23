"""Canonical SQLite memories, namespaces, provenance and deletion tombstones."""
import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from .config import MemoryConfig
from .foundation import FoundationStore, PERSONAL


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(text):
    return hashlib.sha256(re.sub(r"\s+", "", text).casefold().encode()).hexdigest()


def canonical_memory(text):
    """Normalize small presentational differences without guessing semantics."""
    value = unicodedata.normalize("NFKC", text).casefold()
    value = re.sub(r"[\s，。！？、；：,.!?;:'\"“”‘’（）()【】\[\]]+", "", value)
    return re.sub(r"^(?:用户|本人|我(?!们))", "", value, count=1)


def terms(text):
    words = set(re.findall(r"[a-z0-9_]+", text.casefold()))
    for segment in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(segment[i:i+2] for i in range(max(1, len(segment)-1)))
    return words


class EngineStore(FoundationStore):
    def __init__(self, settings, *, path=None, engine="langmem", seed=True):
        settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.settings = settings
        self.engine_id = engine
        self.path = path or settings.data_dir / "memories.sqlite"
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("记忆数据库不能是符号链接。")
        with self.connect() as db:
            # Mem0 generation publication spans this config DB and its native
            # management DB. Rollback journals make the attached commit atomic.
            db.execute("PRAGMA journal_mode=" + ("DELETE" if engine == "mem0" else "WAL"))
            db.executescript("""
                CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, value TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS spaces (id TEXT PRIMARY KEY, name TEXT NOT NULL, engine TEXT NOT NULL, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, space_id TEXT NOT NULL, content TEXT NOT NULL, category TEXT NOT NULL,
                    source_thread TEXT, source_run TEXT, source_quote TEXT NOT NULL, manual INTEGER NOT NULL,
                    created TEXT NOT NULL, updated TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                    deleted INTEGER NOT NULL DEFAULT 0, fingerprint TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS memories_space ON memories(space_id, deleted);
                CREATE TABLE IF NOT EXISTS processed (space_id TEXT, run_id TEXT, status TEXT, count INTEGER, updated TEXT,
                    PRIMARY KEY (space_id, run_id));
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, memory_id TEXT, action TEXT, created TEXT);
                CREATE TABLE IF NOT EXISTS blocked_hashes (space_id TEXT, hash TEXT, PRIMARY KEY(space_id,hash));
                CREATE TABLE IF NOT EXISTS runtime_status (space_id TEXT PRIMARY KEY, error TEXT, updated TEXT);
            """)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS memory_changes (
                    id INTEGER PRIMARY KEY, memory_id TEXT, space_id TEXT NOT NULL, action TEXT NOT NULL,
                    before_value TEXT, after_value TEXT, source_thread TEXT, run_id TEXT, created TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS changes_memory ON memory_changes(memory_id,id);
                CREATE TABLE IF NOT EXISTS memory_runs (
                    space_id TEXT, run_id TEXT, source_thread TEXT, engine TEXT, status TEXT, reason TEXT,
                    summary TEXT, created TEXT, PRIMARY KEY(space_id,run_id));
                CREATE TABLE IF NOT EXISTS memory_vectors (
                    space_id TEXT, memory_id TEXT, signature TEXT, fingerprint TEXT, vector TEXT,
                    PRIMARY KEY(space_id,memory_id,signature));
            """)
            db.execute("INSERT OR IGNORE INTO config VALUES (1,?,1)", (MemoryConfig().model_dump_json(),))
            if seed:
                db.execute("INSERT OR IGNORE INTO spaces VALUES (?,?,?,?)", ("personal" if engine == "langmem" else "mem0-personal", "个人记忆", engine, now()))
        with self.connect() as db:
            self.migrate_foundation(db)
            if engine == "langmem":
                from .lifecycle import migrate
                migrate(db)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def config(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM config WHERE id=1").fetchone()
        return {**MemoryConfig.model_validate_json(row["value"]).model_dump(), "revision": row["revision"]}

    def save_config(self, config):
        with self.connect() as db:
            db.execute("UPDATE config SET value=?,revision=revision+1 WHERE id=1", (config.model_dump_json(),))
        return self.config()

    def spaces(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("""SELECT s.*,count(m.id) AS count FROM spaces s
                LEFT JOIN memories m ON s.id=m.space_id AND m.deleted=0 AND m.status='active' GROUP BY s.id ORDER BY s.created,s.id""")]

    def space(self, sid):
        from .workspace import space_available
        if not space_available(self.settings,sid):
            raise ValueError("此记忆空间已删除或正在删除。")
        with self.connect() as db:
            row = db.execute("SELECT * FROM spaces WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise ValueError("记忆空间不存在。")
        return dict(row)

    def create_space(self, name, engine=None):
        engine = engine or self.config()["engine"]
        if engine not in {"langmem", "mem0"}:
            raise ValueError("记忆引擎不可用。")
        sid = uuid4().hex
        with self.connect() as db:
            if db.execute("SELECT count(*) FROM spaces").fetchone()[0] >= 30:
                raise ValueError("最多创建 30 个记忆空间。")
            db.execute("INSERT INTO spaces VALUES (?,?,?,?)", (sid, name, engine, now()))
        return self.space(sid)

    def public_retention(self,row):
        row=dict(row)
        if self.engine_id=='langmem':
            import time
            with self.connect() as db:
                policy=db.execute('SELECT expires FROM lifecycle_retention WHERE id=?',(row['id'],)).fetchone()
            row['expires']=policy['expires'] if policy else None
            row['expired']=row['expires'] is not None and row['expires']<=time.time()
        return row

    def list(self, sid, query="", page=1, category="", origin="", *, scope=PERSONAL, memory_type="fact", status="active"):
        if category not in {"", "preference", "fact", "goal", "experience"} or origin not in {"", "auto", "manual"}:
            raise ValueError("记忆筛选条件无效。")
        self.space(sid)
        self.validate_scope(sid, scope)
        if memory_type not in {'fact', 'profile'} or status not in {'active', 'archived', 'all'}:
            raise ValueError('记忆类型或状态无效。')
        # Literal substring search, including Chinese; no SQL wildcard interpretation.
        with self.connect() as db:
            args = (sid, query.casefold(), *scope, memory_type)
            where = "space_id=? AND deleted=0 AND instr(lower(content),?)>0 AND scope_kind=? AND scope_id=? AND memory_type=?"
            if self.global_scope:
                where=where.replace("scope_kind=? AND scope_id=? AND ", "")
                args=(sid,query.casefold(),memory_type)
            if status != 'all':
                where += ' AND status=?'; args += (status,)
            if category:
                where += " AND category=?"; args += (category,)
            if origin:
                where += " AND manual=?"; args += (int(origin == "manual"),)
            total = db.execute(f"SELECT count(*) FROM memories WHERE {where}", args).fetchone()[0]
            rows = db.execute(f"SELECT * FROM memories WHERE {where} ORDER BY updated DESC,rowid DESC LIMIT 30 OFFSET ?",
                              (*args, (page-1)*30)).fetchall()
            recent = db.execute("SELECT status,count,updated FROM processed WHERE space_id=? ORDER BY updated DESC,rowid DESC LIMIT 1", (sid,)).fetchone()
            runtime = db.execute("SELECT error FROM runtime_status WHERE space_id=?", (sid,)).fetchone()
        return {"items": [self.public_retention(r) for r in rows], "total": total, "page": page,
                "retrieval_error":runtime['error'] if runtime else None,
                "last_extraction": dict(recent) if recent else None}

    def retrieval_status(self, sid, error=None):
        with self.connect() as db:
            db.execute("INSERT INTO runtime_status VALUES (?,?,?) ON CONFLICT(space_id) DO UPDATE SET error=excluded.error,updated=excluded.updated", (sid,error,now()))

    def get(self, mid):
        with self.connect() as db:
            row = db.execute('SELECT * FROM memories WHERE id=? AND deleted=0', (mid,)).fetchone()
        if row is None:
            raise ValueError('记忆不存在或已删除。')
        return dict(row)

    def candidates(self, sid, query, limit=20, budget=12000, *, records=None):
        rows = self.active(sid) if records is None else records
        wanted = terms(query)
        # Conditional preferences describe how the assistant should collaborate
        # in a matching situation.  Rank their applicability terms ahead of
        # historical goals that merely repeat the same task nouns.
        def score(r):
            content_matches = len(wanted & terms(r["content"]))
            condition_matches = len(wanted & terms(r.get("conditions", "")))
            preference = 2 if r["category"] == "preference" else 0
            return content_matches + condition_matches * 3 + preference
        rows = sorted((r for r in rows if score(r) > 0), key=score, reverse=True)
        result, used = [], 0
        for row in rows:
            size = len(row["content"]) + 100
            if used + size > budget:
                continue
            result.append(row); used += size
            if len(result) >= limit:
                break
        return result

    def active(self, sid, scope=PERSONAL, *, memory_type='fact', include_personal=False):
        self.validate_scope(sid, scope)
        scopes = [scope]
        if include_personal and scope != PERSONAL:
            scopes.append(PERSONAL)
        with self.connect() as db:
            clause = ' OR '.join('(scope_kind=? AND scope_id=?)' for _ in scopes)
            args = (sid, memory_type, *(part for pair in scopes for part in pair))
            if self.global_scope: clause,args="1=1",(sid,memory_type)
            rows = [dict(r) for r in db.execute(f"SELECT * FROM memories WHERE space_id=? AND memory_type=? AND deleted=0 AND status='active' AND ({clause}) ORDER BY updated DESC,rowid DESC", args)]
            if self.engine_id == "langmem":
                from .lifecycle import unavailable
                rows = [r for r in rows if not unavailable(db,r["id"])]
            return rows

    def create(self, sid, body):
        self.validate_metadata(sid, body)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if body.memory_type == 'profile':
                if db.execute("SELECT 1 FROM memories WHERE space_id=? AND memory_type='profile' AND profile_key=? AND deleted=0", (sid, body.profile_key)).fetchone():
                    raise ValueError('档案字段已存在，请刷新后编辑。')
                db.execute('DELETE FROM memory_blocked_topics WHERE space_id=? AND profile_key=?', (sid, body.profile_key))
            self.touch_epoch(db, sid)
            return self._insert(db, sid, body.content, body.category, manual=True, metadata=body.model_dump())

    def _insert(self, db, sid, content, category, *, manual=False, thread=None, run=None, quote="", metadata=None, confirmed=False):
        metadata = metadata or {}
        mid, stamp = uuid4().hex, now()
        db.execute("""INSERT INTO memories(id,space_id,content,category,source_thread,source_run,source_quote,manual,created,updated,version,deleted,fingerprint,
                   memory_type,profile_key,scope_kind,scope_id,status,conditions,topic_key,locked,source_kind)
                   VALUES (?,?,?,?,?,?,?,?,?,?,1,0,?,?,?,?,?,'active',?,?,?,?)""",
                   (mid, sid, content, category, thread, run, quote, int(manual), stamp, stamp, fingerprint(content),
                    metadata.get('memory_type','fact'),metadata.get('profile_key',''),metadata.get('scope_kind','personal'),metadata.get('scope_id','personal'),
                    metadata.get('conditions',''), metadata.get('topic_key',''),int(metadata.get('locked',manual)), 'user_confirmed' if confirmed else 'manual' if manual else 'user_message'))
        db.execute("INSERT INTO events(memory_id,action,created) VALUES (?,?,?)", (mid, "manual_add" if manual else "extract", stamp))
        row = dict(db.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone())
        self._change(db, row, "HOT_ADD" if confirmed else "MANUAL_ADD" if manual else "ADD", thread=thread, run=run)
        return row

    def edit(self, mid, body):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            before = db.execute("SELECT * FROM memories WHERE id=? AND deleted=0", (mid,)).fetchone()
            if before is None:
                raise ValueError('记忆不存在或已删除。')
            merged = {k: before[k] for k in ('memory_type','profile_key','scope_kind','scope_id','conditions','topic_key','locked')}
            merged.update(body.model_dump(exclude_unset=True))
            merged['locked'] = body.locked
            from .config import MemoryInput
            effective = MemoryInput.model_validate({k:v for k,v in merged.items() if k != 'version'})
            self.validate_metadata(before['space_id'], effective)
            if any(getattr(effective,k) != before[k] for k in ('memory_type','profile_key','scope_kind','scope_id')):
                raise ValueError('编辑不能改变记忆类型或范围，请在目标范围新建。')
            changed = db.execute("""UPDATE memories SET content=?,category=?,manual=?,locked=?,conditions=?,topic_key=?,source_kind='manual',updated=?,version=version+1,fingerprint=? WHERE id=? AND version=? AND deleted=0""",
                                (body.content, body.category, int(effective.locked), int(effective.locked), effective.conditions,effective.topic_key,now(),fingerprint(body.content),mid,body.version))
            if not changed.rowcount:
                raise ValueError("记忆已变化或已删除，请刷新后再编辑。")
            db.execute("INSERT INTO events(memory_id,action,created) VALUES (?,'edit',?)", (mid, now()))
            row = dict(db.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone())
            self.touch_epoch(db, row["space_id"])
            self._change(db, row, "MANUAL_UPDATE", before=dict(before))
            db.execute("DELETE FROM memory_vectors WHERE memory_id=?", (mid,))
            db.execute("UPDATE memory_reviews SET status='stale',resolved=? WHERE target_id=? AND status='pending'", (now(), mid))
            return row

    def delete(self, mid):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM memories WHERE id=? AND deleted=0", (mid,)).fetchone()
            if row is None:
                raise ValueError("记忆不存在或已删除。")
            db.execute("INSERT OR IGNORE INTO blocked_hashes VALUES (?,?)", (row["space_id"], row["fingerprint"]))
            db.execute("UPDATE memories SET deleted=1,status='deleted',content='',source_quote='',conditions='',topic_key='',version=version+1,updated=? WHERE id=?", (now(), mid))
            db.execute("INSERT INTO events(memory_id,action,created) VALUES (?,'delete',?)", (mid, now()))
            # History is not a back door to deleted text or provenance.
            db.execute("UPDATE memory_changes SET before_value=NULL,after_value=NULL WHERE memory_id=?", (mid,))
            db.execute("DELETE FROM memory_vectors WHERE memory_id=?", (mid,))
            db.execute('DELETE FROM memory_sources WHERE memory_id=?', (mid,))
            db.execute("UPDATE memory_reviews SET proposal=NULL,status='deleted',resolved=? WHERE target_id=?", (now(),mid))
            db.execute('INSERT INTO memory_epochs VALUES (?,1) ON CONFLICT(space_id) DO UPDATE SET epoch=epoch+1', (row['space_id'],))
            if row['memory_type'] == 'profile':
                db.execute('INSERT OR IGNORE INTO memory_blocked_topics VALUES (?,?)', (row['space_id'],row['profile_key']))
            self._change(db, dict(row), "DELETE", redact=True)

    def processed(self, sid, run):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM processed WHERE space_id=? AND run_id=?", (sid, run)).fetchone() is not None

    @staticmethod
    def _snapshot(row):
        return {k: row[k] for k in ("content", "category", "version", "source_quote", "manual", "memory_type", "profile_key", "scope_kind", "scope_id", "status", "conditions", "locked", "source_kind") if k in row}

    def _change(self, db, row, action, *, before=None, thread=None, run=None, redact=False):
        if not redact:
            self.source(db, row)
        db.execute("INSERT INTO memory_changes(memory_id,space_id,action,before_value,after_value,source_thread,run_id,created) VALUES (?,?,?,?,?,?,?,?)",
                   (row['id'], row['space_id'], action,
                    json.dumps(self._snapshot(before), ensure_ascii=False) if before and not redact else None,
                    json.dumps(self._snapshot(row), ensure_ascii=False) if not redact else None,
                    thread, run, now()))

    def history(self, mid, page=1):
        row = self.get(mid)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM memory_changes WHERE memory_id=? ORDER BY id DESC LIMIT 30 OFFSET ?", (mid,(page-1)*30)).fetchall()
            total = db.execute("SELECT count(*) FROM memory_changes WHERE memory_id=?", (mid,)).fetchone()[0]
        changes = []
        for change in rows:
            item = dict(change)
            for key in ('before_value','after_value'):
                item[key] = json.loads(item[key]) if item[key] else None
            changes.append(item)
        with self.connect() as db:
            sources = [dict(r) for r in db.execute('SELECT * FROM memory_sources WHERE memory_id=? ORDER BY version DESC LIMIT 30 OFFSET ?', (mid,(page-1)*30))]
        return {'memory':row, 'items':changes, 'sources':sources, 'total':total, 'page':page}

    def runs(self, sid, page=1):
        self.space(sid)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM memory_runs WHERE space_id=? ORDER BY created DESC,rowid DESC LIMIT 30 OFFSET ?", (sid,(page-1)*30)).fetchall()
            total = db.execute("SELECT count(*) FROM memory_runs WHERE space_id=?", (sid,)).fetchone()[0]
        return {'items':[{**dict(r), 'summary':json.loads(r['summary'])} for r in rows], 'total':total, 'page':page}

    def _run(self, db, sid, run, status, reason, summary, thread=None):
        engine = db.execute("SELECT engine FROM spaces WHERE id=?", (sid,)).fetchone()['engine']
        db.execute("INSERT OR IGNORE INTO memory_runs VALUES (?,?,?,?,?,?,?,?)",
                   (sid,run,thread,engine,status,reason,json.dumps(summary),now()))

    def finish(self, sid, run, status, count=0, *, reason="", thread=None):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO processed VALUES (?,?,?,?,?)", (sid, run, status, count, now()))
            self._run(db,sid,run,status,reason,{},thread)

    def apply(self, sid, run, thread, text, facts, existing, revision):
        """Commit engine proposals atomically, retaining ADD/UPDATE/NONE semantics."""
        from .mem0_schema import Mem0Fact as RememberedFact
        summary = {"added":0,"updated":0,"unchanged":0,"rejected":0}
        versions = {r["id"]: r["version"] for r in existing}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cfg = db.execute("SELECT * FROM config WHERE id=1").fetchone()
            if cfg["revision"] != revision or not json.loads(cfg["value"])["learn_memories"]:
                self._run(db,sid,run,'skipped','config_changed',summary,thread)
                return {"status": "skipped", "count": 0}
            if db.execute("SELECT 1 FROM processed WHERE space_id=? AND run_id=?", (sid, run)).fetchone():
                return {"status": "skipped", "count": 0}
            changes = 0
            for candidate in facts:
                event = candidate.get('event')
                if event == 'NONE':
                    summary['unchanged'] += 1
                    continue
                if changes >= 5:
                    summary['rejected'] += 1
                    continue
                changes += 1
                try:
                    fact = RememberedFact.model_validate(candidate)
                except ValueError:
                    summary['rejected'] += 1
                    continue
                if event not in {None, 'ADD', 'UPDATE'} or fact.source_quote not in text or not fact.content.strip():
                    summary['rejected'] += 1
                    continue
                mid = candidate.get("id")
                digest = fingerprint(fact.content)
                if db.execute("SELECT 1 FROM blocked_hashes WHERE space_id=? AND hash=?", (sid, digest)).fetchone():
                    summary['rejected'] += 1
                    continue
                if db.execute("SELECT 1 FROM memories WHERE space_id=? AND fingerprint=? AND deleted=0", (sid, digest)).fetchone():
                    summary['unchanged'] += 1
                    continue
                # Never reinterpret an unknown/stale UPDATE as a new ADD.
                if event == 'UPDATE' and mid not in versions:
                    summary['rejected'] += 1
                    continue
                if mid in versions:
                    before = db.execute("SELECT * FROM memories WHERE id=? AND space_id=? AND version=? AND deleted=0 AND manual=0",
                                        (mid,sid,versions[mid])).fetchone()
                    if before is None:
                        summary['rejected'] += 1
                        continue
                    db.execute("""UPDATE memories SET content=?,category=?,source_thread=?,source_run=?,source_quote=?,
                        updated=?,version=version+1,fingerprint=? WHERE id=?""",
                        (fact.content, fact.category, thread, run, fact.source_quote, now(), digest, mid))
                    row = dict(db.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone())
                    self._change(db,row,'UPDATE',before=dict(before),thread=thread,run=run)
                    db.execute("DELETE FROM memory_vectors WHERE memory_id=?", (mid,))
                    db.execute("INSERT INTO events(memory_id,action,created) VALUES (?,'update',?)", (mid, now()))
                    summary['updated'] += 1
                else:
                    self._insert(db, sid, fact.content, fact.category, thread=thread, run=run, quote=fact.source_quote)
                    summary['added'] += 1
            count = summary['added'] + summary['updated']
            db.execute("INSERT INTO processed VALUES (?,?,?,?,?)", (sid, run, "completed", count, now()))
            reason = 'changes_applied' if count else 'candidates_rejected' if summary['rejected'] else 'no_change' if summary['unchanged'] else 'no_facts'
            self._run(db,sid,run,'completed',reason,summary,thread)
        return {"status": "completed", "count": count, "summary": summary}


# Public routing boundary; engine stores never share a writable database.
from .router_store import MemoryStore
