"""P1 profile metadata, atomic activation and cross-process commit fencing.

The registry contains no memory text. Formal records live in engine-owned files.
All activation and memory mutations acquire the same process + file lock.
"""
import fcntl
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

LOCK = threading.RLock()
LOCAL = threading.local()
PROFILES = {"langmem": "langmem-default", "mem0": "mem0-default"}


class Registry:
    def __init__(self, settings):
        self.settings = settings
        self.root = settings.data_dir
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "memory-registry.sqlite"
        if self.path.is_symlink():
            raise ValueError("记忆注册表不能是符号链接。")

    @contextmanager
    def guard(self):
        key = str(self.root)
        with LOCK:
            held = getattr(LOCAL, "held", set())
            if key in held:
                yield
                return
            with (self.root / "memory-registry.lock").open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                LOCAL.held = held | {key}
                try:
                    yield
                finally:
                    LOCAL.held = held
                    fcntl.flock(handle, fcntl.LOCK_UN)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def directory(self, engine):
        if engine not in PROFILES:
            raise ValueError("记忆方案不存在。")
        root = self.root / "memory-profiles" / engine / PROFILES[engine]
        if any(path.is_symlink() for path in (root, root.parent, root.parent.parent)):
            raise ValueError("记忆方案目录不能是符号链接。")
        return root

    def initialize(self):
        from .migration import migrate
        with self.guard():
            if not self.path.exists():
                migrate(self)

    def state(self):
        with self.connect() as db:
            row = dict(db.execute("SELECT * FROM activation WHERE id=1").fetchone())
            profiles = [dict(r) for r in db.execute("SELECT * FROM profiles ORDER BY engine")]
        for p in profiles:
            p["capabilities"] = json.loads(p["capabilities"])
            if p['engine']=='langmem':
                p['implementation']='langmem-p5'
                p['capabilities']=list(dict.fromkeys([*p['capabilities'],'hot_path','persistent_learning','episodes','procedural_rules','lifecycle','context_revocation','resumable_index','verified_backup']))
            if p['engine']=='mem0':
                path=self.directory('mem0')/'native'/'management.sqlite'
                if path.exists():
                    with sqlite3.connect(f'file:{path}?mode=ro',uri=True) as native:
                        ready=native.execute("SELECT value FROM meta WHERE key='published'").fetchone()
                    if ready and ready[0]=='true':
                        p['implementation']='mem0-native-p7'
                        from .mem0_channels import capabilities
                        p['capability_details']=capabilities()
                        p['capabilities']=['category_management','channel_isolation','native_memories','native_history','governed_writes','durable_ingestion','reconciliation','recent_turns','conversation_context','context_exclusions','generation_migration','snapshot_restore','usage_diagnostics']
            p["active"] = p["id"] == row["active_profile_id"]
        return {**row, "profiles": profiles}

    def engine(self, profile_id):
        engine = next((e for e, pid in PROFILES.items() if pid == profile_id), None)
        if engine is None:
            raise ValueError("记忆方案不存在。")
        return engine

    def valid(self, frozen):
        state = self.state()
        return (frozen.get("memory_profile_id") == state["active_profile_id"]
                and frozen.get("activation_epoch") == state["epoch"])

    def require_active(self, profile_id):
        if self.state()["active_profile_id"] != profile_id:
            raise ValueError("此记忆方案未激活，请先切回对应方案。")

    def activate(self, profile_id, check=None, new_space=None, space_id=None):
        engine = self.engine(profile_id)
        with self.guard():
            # Validate before touching the active pointer; failures retain it.
            if check:
                check()
            with self.connect() as db:
                if new_space:
                    from .store import now
                    import re
                    sid, name = new_space['id'], new_space['name'].strip()
                    if not re.fullmatch(r'[a-f0-9]{32}', sid) or not 1 <= len(name) <= 80:
                        raise ValueError('新记忆空间名称或标识无效。')
                    if new_space.get('engine') not in (None, engine):
                        raise ValueError('新记忆空间必须属于目标引擎。')
                    for other in PROFILES:
                        if other == engine:
                            continue
                        with sqlite3.connect(f"file:{self.directory(other) / 'store.sqlite'}?mode=ro", uri=True) as other_db:
                            if other_db.execute('SELECT 1 FROM spaces WHERE id=?', (sid,)).fetchone():
                                raise ValueError('新记忆空间请求已改变，请重新选择。')
                    path = self.directory(engine) / 'store.sqlite'
                    if path.is_symlink():
                        raise ValueError('记忆数据库不能是符号链接。')
                    db.execute('ATTACH DATABASE ? AS target', (str(path),))
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('CREATE TABLE IF NOT EXISTS fresh_starts(id TEXT PRIMARY KEY,profile_id TEXT NOT NULL,name TEXT NOT NULL)')
                    receipt = db.execute('SELECT profile_id,name FROM fresh_starts WHERE id=?', (sid,)).fetchone()
                    if receipt and tuple(receipt) != (profile_id, name):
                        raise ValueError('新记忆空间请求已改变，请重新选择。')
                    existing = db.execute('SELECT name,engine FROM target.spaces WHERE id=?', (sid,)).fetchone()
                    if existing:
                        if tuple(existing) != (name, engine) or not receipt or tuple(receipt) != (profile_id, name):
                            raise ValueError('新记忆空间请求已改变，请重新选择。')
                    else:
                        if db.execute('SELECT count(*) FROM target.spaces'+(" WHERE id NOT IN (SELECT id FROM main.removed_memory_spaces WHERE state='deleted')" if db.execute("SELECT 1 FROM sqlite_master WHERE name='removed_memory_spaces'").fetchone() else '')).fetchone()[0] >= 30:
                            raise ValueError('最多创建 30 个记忆空间。')
                        db.execute('INSERT INTO target.spaces VALUES (?,?,?,?)', (sid, name, engine, now()))
                        db.execute('INSERT OR IGNORE INTO fresh_starts VALUES (?,?,?)', (sid, profile_id, name))
                if db.execute("SELECT active_profile_id FROM activation WHERE id=1").fetchone()[0] != profile_id:
                    db.execute("UPDATE activation SET active_profile_id=?,epoch=epoch+1 WHERE id=1", (profile_id,))
            from .workspace import managed, INITIAL
            if managed(self.settings):
                chosen = new_space["id"] if new_space else space_id
                with self.connect() as prefs:
                    if chosen is None: chosen=prefs.execute("SELECT space_id FROM workspace_spaces WHERE engine=?",(engine,)).fetchone()[0]
                # Space validity is checked by the web boundary before activation.
                with self.connect() as prefs:
                    prefs.execute("UPDATE workspace_spaces SET space_id=? WHERE engine=?",(chosen or INITIAL[engine],engine))
        result = self.state()
        if new_space:
            result['space_id'] = new_space['id']
        return result

    def set_default(self, profile_id):
        self.engine(profile_id)
        with self.guard(), self.connect() as db:
            db.execute("UPDATE activation SET default_profile_id=? WHERE id=1", (profile_id,))
        return self.state()

    def bind(self, thread_id, frozen):
        """One immutable profile per chat, independent of per-turn preferences."""
        profile_id = frozen["memory_profile_id"]
        with self.connect() as db:
            old = db.execute("SELECT profile_id FROM sessions WHERE thread_id=?", (thread_id,)).fetchone()
            if old and old[0] != profile_id:
                raise ValueError("切换记忆方案需要新建对话，原对话保留。")
            db.execute("INSERT OR IGNORE INTO sessions VALUES (?,?)", (thread_id, profile_id))

    def binding(self, thread_id):
        with self.connect() as db:
            row = db.execute("SELECT profile_id FROM sessions WHERE thread_id=?", (thread_id,)).fetchone()
        return row[0] if row else None
