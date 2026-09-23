"""Mem0-owned governance. Memory bodies remain authoritative in native Qdrant."""
import json
import sqlite3
import time
from contextlib import contextmanager
from uuid import uuid4

PROFILE='mem0-default'
TERMINAL=('completed','cancelled','skipped','reconciled')


class NativeStore:
    def __init__(self,registry,channel="ordinary"):
        from .mem0_channels import kind
        self.channel=kind(channel)
        self.registry=registry;self.base_root=registry.directory('mem0')/'native'
        self.root=self.base_root if channel=='ordinary' else self.base_root/'channels'/channel
        if self.base_root.is_symlink() or (self.base_root/'channels').is_symlink():raise ValueError('Mem0 原生目录不能是符号链接。')
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.root.is_symlink():raise ValueError('Mem0 原生目录不能是符号链接。')
        self.path=self.root/'management.sqlite'
        if self.path.is_symlink():raise ValueError('Mem0 管理库不能是符号链接。')
        with self.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS methods(id TEXT PRIMARY KEY,space_id TEXT,key TEXT,title TEXT,version INTEGER,active_id TEXT,previous_id TEXT,candidate_id TEXT,enabled INTEGER,locked INTEGER,state TEXT,updated REAL);
              CREATE UNIQUE INDEX IF NOT EXISTS methods_key ON methods(space_id,key) WHERE state!='deleted';
              CREATE TABLE IF NOT EXISTS method_revisions(native_id TEXT PRIMARY KEY,method_id TEXT,source_event TEXT,event_version INTEGER);
              CREATE TABLE IF NOT EXISTS method_blocks(space_id TEXT,key TEXT,PRIMARY KEY(space_id,key));
              CREATE TABLE IF NOT EXISTS category_batches(id TEXT PRIMARY KEY,space_id TEXT,category TEXT,state TEXT,request_hash TEXT,plan TEXT,cursor INTEGER,frozen TEXT,created REAL,updated REAL);
              CREATE UNIQUE INDEX IF NOT EXISTS one_category_batch ON category_batches(space_id) WHERE state!='completed';
              CREATE TABLE IF NOT EXISTS contexts(thread_id TEXT PRIMARY KEY,space_id TEXT,version INTEGER,summary TEXT,request TEXT,task TEXT,sources TEXT,budget TEXT,error TEXT,updated REAL,epoch INTEGER);
              CREATE TABLE IF NOT EXISTS context_exclusions(space_id TEXT,thread_id TEXT,turn_id TEXT,run_id TEXT,cutoff REAL,PRIMARY KEY(space_id,thread_id,turn_id));
              CREATE TABLE IF NOT EXISTS context_barriers(space_id TEXT,thread_id TEXT,cutoff REAL,PRIMARY KEY(space_id,thread_id));
              CREATE TABLE IF NOT EXISTS native_usage(kind TEXT,calls INTEGER,tokens INTEGER,unknown INTEGER,failed INTEGER,seconds REAL,created REAL);
              CREATE TABLE IF NOT EXISTS generations(id TEXT PRIMARY KEY,state TEXT,source TEXT,target TEXT,baseline TEXT,done INTEGER,total INTEGER,dimensions INTEGER,error TEXT,created REAL);
              CREATE TABLE IF NOT EXISTS generation_items(generation TEXT,memory_id TEXT,PRIMARY KEY(generation,memory_id));
              CREATE TABLE IF NOT EXISTS backups(id TEXT PRIMARY KEY,state TEXT,digest TEXT,created REAL,items INTEGER);
              CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
              CREATE TABLE IF NOT EXISTS controls(id TEXT PRIMARY KEY,space_id TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,locked INTEGER NOT NULL,source_thread TEXT,source_run TEXT,created REAL,updated REAL);
              CREATE INDEX IF NOT EXISTS controls_space ON controls(space_id,state);
              CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,space_id TEXT,kind TEXT,state TEXT,reason TEXT,thread_id TEXT,run_id TEXT,frozen TEXT,source TEXT,created REAL,updated REAL,UNIQUE(space_id,thread_id,run_id,kind));
              CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY,operation_id TEXT,memory_id TEXT,event TEXT,state TEXT,old TEXT,payload TEXT,created REAL);
              CREATE TABLE IF NOT EXISTS interventions(id TEXT PRIMARY KEY,space_id TEXT,operation_id TEXT,memory_id TEXT,event TEXT,reason TEXT,proposal TEXT,state TEXT,created REAL);
              CREATE TABLE IF NOT EXISTS tombstones(space_id TEXT,memory_id TEXT,hash TEXT,thread_id TEXT,run_id TEXT,created REAL,PRIMARY KEY(space_id,memory_id));
              CREATE TABLE IF NOT EXISTS blocked_hashes(space_id TEXT,hash TEXT,PRIMARY KEY(space_id,hash));
              CREATE TABLE IF NOT EXISTS source_barriers(space_id TEXT,thread_id TEXT,run_id TEXT,PRIMARY KEY(space_id,thread_id,run_id));
              CREATE TABLE IF NOT EXISTS native_sources(memory_id TEXT,thread_id TEXT,run_id TEXT,PRIMARY KEY(memory_id,thread_id,run_id));
              CREATE TABLE IF NOT EXISTS id_map(legacy_id TEXT PRIMARY KEY,native_id TEXT UNIQUE,space_id TEXT,version INTEGER,history TEXT);
              CREATE TABLE IF NOT EXISTS cursors(space_id TEXT,thread_id TEXT,last_operation TEXT,PRIMARY KEY(space_id,thread_id));
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=20);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def meta(self,key):
        with self.connect() as db:r=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return json.loads(r[0]) if r else None

    def set_meta(self,key,value):
        with self.connect() as db:db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,json.dumps(value)))

    @property
    def ready(self):return self.meta('published') is True

    def batch(self,sid):
        with self.connect() as db:
            row=db.execute("SELECT id,category,state FROM category_batches WHERE space_id=? AND state!='completed'",(sid,)).fetchone()
        return dict(row) if row else None

    def require_no_batch(self,sid):
        if self.batch(sid):raise ValueError('此空间的分类保存尚未完成，请在我的记忆中重试。')

    def unresolved(self,sid):
        with self.connect() as db:return bool(db.execute("SELECT 1 FROM operations WHERE space_id=? AND state IN ('running','needs_reconcile')",(sid,)).fetchone())

    def operation(self,oid):
        with self.connect() as db:r=db.execute('SELECT * FROM operations WHERE id=?',(oid,)).fetchone()
        if not r:raise ValueError('Mem0 操作不存在。')
        return dict(r)

    def control(self,mid):
        with self.connect() as db:r=db.execute('SELECT * FROM controls WHERE id=?',(mid,)).fetchone()
        if not r:raise ValueError('Mem0 记忆不存在。')
        return dict(r)

    def start(self,sid,kind,source,frozen,thread='',run='',state='running'):
        oid=uuid4().hex;stamp=time.time()
        with self.connect() as db:
            prior=db.execute('SELECT id FROM operations WHERE space_id=? AND thread_id=? AND run_id=? AND kind=?',(sid,thread,run,kind)).fetchone() if run else None
            if prior:return prior[0]
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?)',(oid,sid,kind,state,'',thread,run,json.dumps(frozen),source,stamp,stamp))
        return oid

    def finish(self,oid,state,reason=''):
        with self.connect() as db:
            db.execute('UPDATE operations SET state=?,reason=?,source=CASE WHEN ? THEN \'\' ELSE source END,updated=? WHERE id=?',(state,reason,int(state in TERMINAL),time.time(),oid))
            row=db.execute('SELECT * FROM operations WHERE id=?',(oid,)).fetchone()
            # Advance across the entire terminal prefix, even if a later job
            # completed before this earlier barrier was resolved.
            prefix=db.execute("SELECT id FROM operations o WHERE space_id=? AND thread_id=? AND state IN ('completed','cancelled','skipped','reconciled') AND NOT EXISTS(SELECT 1 FROM operations p WHERE p.space_id=o.space_id AND p.thread_id=o.thread_id AND p.created<=o.created AND p.state NOT IN ('completed','cancelled','skipped','reconciled')) ORDER BY created DESC LIMIT 1",(row['space_id'],row['thread_id'])).fetchone()
            if prefix:db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?,?)',(row['space_id'],row['thread_id'],prefix[0]))

    def intervention(self,op,mid,event,reason,text):
        with self.connect() as db:db.execute('INSERT INTO interventions VALUES (?,?,?,?,?,?,?,?,?)',(uuid4().hex,op['space_id'],op['id'],mid,event,reason,text,'pending',time.time()))
