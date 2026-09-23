"""Authoritative state for workflow/team runs and their durable stage results.

The outer scheduler is deliberately stateless between process runs. Recovery,
retry, approval and completion decisions must be derived from this store. Child
Agents keep their own LangGraph checkpoints in their isolated run directories.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4


def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def encode(value): return json.dumps(value, ensure_ascii=False)


class BudgetExceeded(ValueError):
    def __init__(self, reason, row, config, estimate):
        self.details = {'reason':reason,'used_tokens':row['tokens'],'max_tokens':config['max_tokens'],
                        'model_calls':row['calls'],'max_model_calls':config['max_model_calls'],
                        'next_reservation':estimate,'usage_unknown':bool(row['unknown'])}
        super().__init__('流程模型调用次数已达上限。' if reason=='calls' else
                         '流程剩余预算不足以预留下一次模型调用。')


class WorkflowStore:
    filename = "workflows.sqlite"
    source = "workflow"
    execution_mode = "fixed"

    def __init__(self, settings):
        self.path = settings.data_dir / self.filename
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS definitions(id TEXT PRIMARY KEY, enabled INTEGER, archived INTEGER);
                CREATE TABLE IF NOT EXISTS versions(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT, version TEXT, definition TEXT, UNIQUE(id,version));
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, thread_id TEXT, request TEXT, snapshot TEXT, status TEXT, output TEXT DEFAULT '', quality TEXT DEFAULT 'pending', rounds TEXT DEFAULT '{}', calls INTEGER DEFAULT 0, tokens INTEGER DEFAULT 0, unknown INTEGER DEFAULT 0, created TEXT);
                CREATE TABLE IF NOT EXISTS stages(run_id TEXT, node_id TEXT, attempt INTEGER, data TEXT, PRIMARY KEY(run_id,node_id,attempt));
            ''')
            self._mark_interrupted(db)

    @staticmethod
    def _mark_interrupted(db):
        """Record an unclean process stop without consuming a retry attempt."""
        interrupted = now()
        running = {row['id'] for row in db.execute("SELECT id FROM runs WHERE status='running'")}
        if not running:
            return
        db.execute("UPDATE runs SET status='interrupted' WHERE status='running'")
        rows = db.execute('SELECT run_id,node_id,attempt,data FROM stages').fetchall()
        for row in rows:
            data = json.loads(row['data'])
            if row['run_id'] not in running or data.get('status') != 'running':
                continue
            data.update(
                status='interrupted',
                error='阶段在服务重启时中断，将从已保存步骤继续。',
                updated=interrupted,
            )
            db.execute(
                'UPDATE stages SET data=? WHERE run_id=? AND node_id=? AND attempt=?',
                (encode(data), row['run_id'], row['node_id'], row['attempt']),
            )

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30); db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def get(self, wid):
        with self.db() as db:
            row = db.execute('SELECT * FROM definitions WHERE id=?',(wid,)).fetchone()
            if row is None: raise ValueError('流程不存在。')
            versions = [json.loads(r[0]) for r in db.execute('SELECT definition FROM versions WHERE id=? ORDER BY seq DESC',(wid,))]
        versions = [{**d,'version_number':len(versions)-i} for i,d in enumerate(versions)]
        return {**versions[0], 'enabled':bool(row['enabled']), 'archived':bool(row['archived']), 'revisions':versions}

    def list(self):
        with self.db() as db: ids = [r[0] for r in db.execute('SELECT id FROM definitions ORDER BY rowid DESC')]
        return [self.get(wid) for wid in ids]

    def save(self, body, wid=None):
        if wid:
            current = self.get(wid)
            if all(current.get(key)==value for key,value in body.model_dump().items()): return current
        wid = wid or self.source+'-'+uuid4().hex
        data = {**body.model_dump(), 'id':wid, 'version':uuid4().hex, 'source':self.source, 'execution_mode':self.execution_mode}
        with self.db() as db:
            db.execute('INSERT INTO definitions VALUES (?,1,0) ON CONFLICT(id) DO NOTHING',(wid,))
            db.execute('INSERT INTO versions(id,version,definition) VALUES (?,?,?)',(wid,data['version'],encode(data)))
        return self.get(wid)

    def status(self, wid, changes):
        self.get(wid)
        with self.db() as db:
            for key in ('enabled','archived'):
                if key in changes: db.execute(f'UPDATE definitions SET {key}=? WHERE id=?',(int(changes[key]),wid))
        return self.get(wid)

    def begin(self, prepared):
        req, snapshot = prepared.request, prepared.snapshot
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO runs(id,thread_id,request,snapshot,status,created) VALUES (?,?,?,?,?,?)',
                       (snapshot['run_id'],req['thread_id'],encode(req),encode(snapshot),'running',now()))
            # Resume may refresh only the active memory epoch after the user
            # switches away and explicitly returns to the bound engine. Keep
            # the durable workflow/team snapshot aligned before any stage
            # reloads it from this store.
            db.execute('UPDATE runs SET snapshot=? WHERE id=?',(encode(snapshot),snapshot['run_id']))
            for node in snapshot['workflow']['nodes']:
                data = {'node_id':node['id'],'name':node['name'],'kind':node['kind'],'expert':node['expert'],
                        'expert_name':snapshot['experts'].get(node['expert'],{}).get('name',''),
                        'attempt':1,'status':'pending','output':'','sources':[],'records':[],'error':'','approved':None}
                db.execute('INSERT OR IGNORE INTO stages VALUES (?,?,?,?)',(snapshot['run_id'],node['id'],1,encode(data)))

    def run(self, rid):
        with self.db() as db: row = db.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
        if row is None: raise ValueError('流程运行尚未建立，请重新发起。')
        data = dict(row)
        for key in ('request','snapshot','rounds'): data[key] = json.loads(data[key])
        return data

    def history(self, tid):
        with self.db() as db: ids = [r[0] for r in db.execute('SELECT id FROM runs WHERE thread_id=? ORDER BY rowid',(tid,))]
        return [self.run(rid) for rid in ids]

    def stages(self, rid, all_attempts=False):
        with self.db() as db: rows = [json.loads(r[0]) for r in db.execute('SELECT data FROM stages WHERE run_id=? ORDER BY rowid',(rid,))]
        if all_attempts: return rows
        return dict((r['node_id'],r) for r in rows)

    def stage(self, rid, node, **changes):
        data = {**node, **changes, 'updated':now()}
        with self.db() as db:
            db.execute('INSERT INTO stages VALUES (?,?,?,?) ON CONFLICT(run_id,node_id,attempt) DO UPDATE SET data=excluded.data',
                       (rid,data['node_id'],data['attempt'],encode(data)))
        return data

    def update(self, rid, **changes):
        assert set(changes) <= {'status','output','quality','rounds'}
        with self.db() as db:
            for key,value in changes.items():
                db.execute(f'UPDATE runs SET {key}=? WHERE id=?',(encode(value) if key=='rounds' else value,rid))

    def reserve(self, rid, estimated):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
            config = json.loads(row['snapshot'])['workflow']
            if row['calls'] >= config['max_model_calls']:
                raise BudgetExceeded('calls',row,config,estimated)
            if row['tokens']+estimated > config['max_tokens']:
                raise BudgetExceeded('tokens',row,config,estimated)
            db.execute('UPDATE runs SET calls=calls+1,tokens=tokens+?,unknown=unknown+1 WHERE id=?',(estimated,rid))

    def settle(self, rid, estimate, actual):
        if actual is not None:
            with self.db() as db:
                db.execute('UPDATE runs SET tokens=tokens+?,unknown=unknown-1 WHERE id=?',(actual-estimate,rid))
