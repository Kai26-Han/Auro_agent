"""Restartable split migration. Registry publication is the single cutover point.

Legacy files are retained. Interrupted staging can be rebuilt before publication;
after publication it is never recopied over new records. Reports contain counts,
not user text. Rollback exports *all* live files before disabling V2 externally.
"""
import hashlib
import fcntl
import json
import os
import sqlite3
import tempfile
from dataclasses import replace
from pathlib import Path
from datetime import datetime, timezone

from .registry import PROFILES


def backup(source, target):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    target.chmod(0o600)


def inventory(registry):
    legacy = registry.root / "memories.sqlite"
    if not legacy.exists():
        return {"legacy": False, "engines": {}, "cutover": registry.path.exists()}
    with sqlite3.connect(f"file:{legacy}?mode=ro", uri=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='spaces'").fetchone():
            return {"legacy":True, "engines":{"langmem":db.execute("SELECT count(*) FROM memories").fetchone()[0]}, "cutover":registry.path.exists()}
        rows = db.execute("SELECT s.engine,count(m.id) FROM spaces s LEFT JOIN memories m ON m.space_id=s.id GROUP BY s.engine").fetchall()
    return {"legacy": True, "engines": dict(rows), "cutover": registry.path.exists()}


def dry_run(registry):
    """Run the actual split and integrity verification on a temporary clone."""
    from .registry import Registry
    report = inventory(registry)
    if report['cutover']:
        return {**report, 'action':'already_migrated'}
    with tempfile.TemporaryDirectory(prefix='memory-p1-dry-run-') as folder:
        test = Registry(replace(registry.settings, project_dir=Path(folder)))
        source = registry.root / 'memories.sqlite'
        if source.exists():
            backup(source, test.root / 'memories.sqlite')
        test.initialize()
        report['verified'] = {}
        for engine in PROFILES:
            with sqlite3.connect(test.directory(engine)/'store.sqlite') as db:
                report['verified'][engine] = {'integrity':db.execute('PRAGMA integrity_check').fetchone()[0],
                                             'records':db.execute('SELECT count(*) FROM memories').fetchone()[0]}
    return report


def digest(db):
    # Includes tombstones, provenance, reviews, user protection and derived cache.
    result = {}
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        rows = sorted((repr(tuple(r)) for r in db.execute(f'SELECT * FROM "{table}"')))
        result[table] = {"count": len(rows), "sha256": hashlib.sha256("\n".join(rows).encode()).hexdigest()}
    return result


def prune(db, engine):
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        cols = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
        if "space_id" in cols and table != "memories":
            db.execute(f'DELETE FROM "{table}" WHERE space_id NOT IN (SELECT id FROM spaces WHERE engine=?)', (engine,))
        elif "memory_id" in cols:
            db.execute(f'DELETE FROM "{table}" WHERE memory_id NOT IN (SELECT m.id FROM memories m JOIN spaces s ON s.id=m.space_id WHERE s.engine=?)', (engine,))
    db.execute("DELETE FROM memories WHERE space_id NOT IN (SELECT id FROM spaces WHERE engine=?)", (engine,))
    db.execute("DELETE FROM spaces WHERE engine!=?", (engine,))


def migrate(registry):
    with (registry.root / 'run.lock').open('a') as handle:
        if (registry.root / 'memories.sqlite').exists():
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('请先停止旧版运行任务，再执行 P1 迁移。') from None
        return _migrate(registry)


def _migrate(registry):
    from .store import EngineStore
    legacy = registry.root / "memories.sqlite"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = registry.root / "backups" / ("before-p1-" + stamp)
    report = inventory(registry)
    if registry.path.exists():
        return report
    if legacy.exists():
        app_path = registry.root / 'app.sqlite'
        if app_path.exists():
            with sqlite3.connect(f'file:{app_path}?mode=ro', uri=True) as db:
                pending = db.execute("SELECT count(*) FROM sessions WHERE status IN ('running','waiting_approval','interrupted','stopped')").fetchone()[0]
            if pending:
                raise ValueError('有未完成的旧会话，请先在旧版完成或恢复任务，再迁移；不会强制重绑审批。')
    # Caller holds the registry lock. Old executors must be stopped before upgrade.
    if legacy.exists():
        backup(legacy, destination / "memories.sqlite")
    reports = {}
    for engine in PROFILES:
        root = registry.directory(engine)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = root / "store.sqlite"
        stage = root / "migration.sqlite"
        for suffix in ("", "-wal", "-shm"):
            stage.with_name(stage.name + suffix).unlink(missing_ok=True)
        if legacy.exists():
            backup(legacy, stage)
        store = EngineStore(registry.settings, path=stage, engine=engine, seed=legacy.exists() and engine == "langmem")
        with store.connect() as db:
            prune(db, engine)
            config = json.loads(db.execute("SELECT value FROM config WHERE id=1").fetchone()[0])
            config["engine"] = engine
            db.execute("UPDATE config SET value=? WHERE id=1", (json.dumps(config),))
            expected = digest(db)
        # Check a fresh connection rather than accepting only the write result.
        with store.connect() as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert digest(db) == expected
            reports[engine] = expected
            if not db.execute("SELECT 1 FROM spaces").fetchone():
                db.execute("INSERT INTO spaces VALUES (?,?,?,?)", ("personal" if engine == "langmem" else "mem0-personal", "个人记忆", engine, stamp))
        with sqlite3.connect(stage) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("PRAGMA journal_mode=DELETE")
        os.replace(stage, target)
    # Build and atomically publish metadata only after both owned stores exist.
    stage_registry = registry.path.with_suffix(".staging")
    stage_registry.unlink(missing_ok=True)
    with sqlite3.connect(stage_registry) as db:
        db.executescript("""
            CREATE TABLE profiles(id TEXT PRIMARY KEY,engine TEXT UNIQUE,implementation TEXT,schema_version INTEGER,capabilities TEXT);
            CREATE TABLE activation(id INTEGER PRIMARY KEY,active_profile_id TEXT,default_profile_id TEXT,epoch INTEGER);
            CREATE TABLE sessions(thread_id TEXT PRIMARY KEY,profile_id TEXT NOT NULL);
        """)
        for engine, pid in PROFILES.items():
            capabilities = ["facts", "recall", "scoped_storage", "summary", "task", "profile"] if engine == "langmem" else ["compat_facts", "semantic_recall", "recent_turns"]
            db.execute("INSERT INTO profiles VALUES (?,?,?,?,?)", (pid, engine, "langmem-lm2" if engine == "langmem" else "mem0-compat", 1, json.dumps(capabilities)))
        db.execute("INSERT INTO activation VALUES (1,?,?,1)", (PROFILES["langmem"], PROFILES["langmem"]))
    stage_registry.chmod(0o600)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    (destination / "report.json").write_text(json.dumps({**report, "verification": reports}, indent=2))
    os.replace(stage_registry, registry.path)
    return {**report, "backup": str(destination)}


def export_rollback(registry, destination):
    """Non-destructive rollback bundle; never replaces V2 with stale legacy data."""
    with registry.guard():
        if destination.exists():
            raise ValueError("回退导出目录已存在，请选择新目录。")
        destination.mkdir(parents=True, mode=0o700)
        backup(registry.path, destination / registry.path.name)
        for engine in PROFILES:
            root = registry.directory(engine)
            for source in root.rglob("*.sqlite"):
                backup(source, destination / engine / source.relative_to(root))
        (destination / "README.txt").write_text("完整保留 P1 新写入数据。旧版不能直接读取独立方案库；勿用旧 memories.sqlite 覆盖。恢复时停服并成套恢复注册表和各方案目录。可重建的向量投影不包含在此包。\n")
