"""LangMem durable source ledger and ordered, fenced background consolidation.

Only newly authorized user turns are ingested. The source, job, cursor and formal
commit share the owned LangMem SQLite transaction. Model work runs outside it.
"""
import fcntl
import json
import re
import sqlite3
import threading
import time
from uuid import uuid4

PROFILE = 'langmem-default'
TERMINAL = ('completed', 'skipped', 'cancelled')
EXCLUDED = re.compile(r"sk-[\w-]{12,}|-----BEGIN .*PRIVATE KEY|(?:api[_ -]?key|password|密码|密钥|令牌)\s*[:=：]|不要记|别记|不记住|忘记|do not remember|don't remember|forget", re.I)


class LearningQueue:
    def __init__(self, memory):
        self.memory = memory
        self.registry = memory.store.registry
        self.store = memory.store.stores['langmem']
        self.episodes = memory.episodes
        self.rules = memory.rules
        with self.store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS learning_jobs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    profile_id TEXT NOT NULL, space_id TEXT NOT NULL, thread_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL, source TEXT NOT NULL, frozen TEXT NOT NULL,
                    state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
                    available REAL NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    source_epoch INTEGER NOT NULL, lease TEXT, lease_until REAL, summary TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(space_id,thread_id,run_id));
                CREATE INDEX IF NOT EXISTS learning_due ON learning_jobs(state,available,seq);
                CREATE TABLE IF NOT EXISTS learning_cursors (
                    space_id TEXT,thread_id TEXT,seq INTEGER NOT NULL,PRIMARY KEY(space_id,thread_id));
                CREATE TABLE IF NOT EXISTS memory_operations (
                    operation_id TEXT PRIMARY KEY,space_id TEXT NOT NULL,thread_id TEXT NOT NULL,run_id TEXT NOT NULL,
                    digest TEXT NOT NULL,result TEXT NOT NULL,created REAL NOT NULL);
            ''')

    def enqueue(self, frozen, text, thread, run, await_answer=False):
        if not frozen or frozen.get('engine') != 'langmem' or not frozen.get('learn_memories'):
            return {'status':'disabled','count':0}
        self.recover_answers()
        now = time.time()
        with self.registry.guard(), self.store.connect() as db:
            if not self.registry.valid(frozen):
                return {'status':'skipped','count':0,'reason':'profile_inactive'}
            cfg = self.store.config()
            if not (cfg.get("enabled",True) and cfg['learn_memories']) or cfg['revision'] != frozen['revision']:
                return {'status':'skipped','count':0,'reason':'config_changed'}
            from .lifecycle import blocked
            if blocked(db,frozen['space_id'],thread,run):
                return {'status':'skipped','count':0,'reason':'source_deleted'}
            existing = db.execute('SELECT id,state FROM learning_jobs WHERE space_id=? AND thread_id=? AND run_id=?', (frozen['space_id'],thread,run)).fetchone()
            if existing:
                return {'status':existing['state'],'count':0,'job_id':existing['id']}
            # A new turn supersedes any abandoned/failed awaiting source.
            for old in db.execute("SELECT * FROM learning_jobs WHERE space_id=? AND thread_id=? AND state='awaiting_answer'", (frozen['space_id'],thread)).fetchall():
                self.finish(db,old,'cancelled','answer_not_completed')
            excluded = bool(EXCLUDED.search(text))
            sid = frozen['space_id']
            epoch = self.store.epoch(sid)
            delay = cfg['langmem']['debounce_seconds']
            jid = uuid4().hex
            # Sliding quiet window, capped at five minutes from each source.
            db.execute("UPDATE learning_jobs SET available=min(created+300,?),updated=? WHERE space_id=? AND thread_id=? AND state='pending'", (now+delay,now,sid,thread))
            db.execute('''INSERT INTO learning_jobs(id,profile_id,space_id,thread_id,run_id,scope_kind,scope_id,
                source,frozen,state,reason,available,created,updated,source_epoch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (jid,PROFILE,sid,thread,run,frozen.get('scope_kind','personal'),frozen.get('scope_id','personal'),
                 '' if excluded else text,json.dumps(frozen),'skipped' if excluded else 'awaiting_answer' if await_answer else 'pending',
                 'excluded_input' if excluded else '',now+delay,now,now,epoch))
            if excluded: self.advance(db,sid,thread)
        return {'status':'skipped' if excluded else 'queued','count':0,'job_id':jid}

    def release(self, jid, completed=True):
        with self.registry.guard(), self.store.connect() as db:
            row = db.execute("SELECT * FROM learning_jobs WHERE id=?",(jid,)).fetchone()
            if row is None: return 'skipped'
            if row['state'] != 'awaiting_answer': return row['state']
            if completed:
                delay = json.loads(row['frozen'])['langmem']['debounce_seconds']
                now = time.time()
                db.execute("UPDATE learning_jobs SET available=min(created+300,?) WHERE space_id=? AND thread_id=? AND state='pending'",(now+delay,row['space_id'],row['thread_id']))
                db.execute("UPDATE learning_jobs SET state='pending',available=?,updated=? WHERE id=?",(now+delay,now,jid))
            else:
                self.finish(db,row,'cancelled','answer_not_completed')
            return 'pending' if completed else 'cancelled'

    def recover_answers(self):
        """Repair the finish→enqueue crash window only for pre-registered sources.

        Read completion metadata, never historical conversation bodies.
        """
        with self.store.connect() as db:
            waiting = db.execute("SELECT id,thread_id,run_id FROM learning_jobs WHERE state='awaiting_answer'").fetchall()
        if not waiting: return
        from personal_workbench.workflows.memory import parent_outcome
        for row in waiting:
            parent = parent_outcome(self.registry.root,row['thread_id'],row['run_id'])
            if parent and parent['terminal']:
                self.release(row['id'],parent['accepted']);continue
            done = None
            path = self.registry.root/'app.sqlite'
            if path.exists():
                with sqlite3.connect(f'file:{path}?mode=ro',uri=True) as db:
                    done = db.execute("SELECT 1 FROM sessions WHERE id=? AND status='completed' AND json_extract(skill_snapshot,'$.run_id')=?",(row['thread_id'],row['run_id'])).fetchone()
            if not done:
                jobs_path = self.registry.root/'web.sqlite'
                if jobs_path.exists():
                    with sqlite3.connect(f'file:{jobs_path}?mode=ro',uri=True) as jobs:
                        done = jobs.execute("SELECT 1 FROM jobs WHERE thread_id=? AND status='completed' AND json_extract(snapshot,'$.run_id')=? LIMIT 1",(row['thread_id'],row['run_id'])).fetchone()
            if done:self.release(row['id'])

    @staticmethod
    def advance(db, sid, thread):
        barrier = db.execute("SELECT min(seq) FROM learning_jobs WHERE space_id=? AND thread_id=? AND state NOT IN ('completed','skipped','cancelled')", (sid,thread)).fetchone()[0]
        seq = db.execute('SELECT max(seq) FROM learning_jobs WHERE space_id=? AND thread_id=? AND (? IS NULL OR seq<?)', (sid,thread,barrier,barrier)).fetchone()[0]
        if seq is not None:
            db.execute('INSERT INTO learning_cursors VALUES (?,?,?) ON CONFLICT(space_id,thread_id) DO UPDATE SET seq=excluded.seq', (sid,thread,seq))

    def finish(self, db, row, state, reason='', summary=None):
        db.execute('UPDATE learning_jobs SET state=?,reason=?,source=?,lease=NULL,lease_until=NULL,summary=?,updated=? WHERE id=?',
                   (state,reason,'' if state in TERMINAL else row['source'],json.dumps(summary or {}),time.time(),row['id']))
        self.advance(db,row['space_id'],row['thread_id'])

    def public(self, sid, page=1):
        self.memory.store.require_langmem(sid)
        active = self.registry.state()['active_profile_id'] == PROFILE
        cfg = self.store.config()
        with self.store.connect() as db:
            total = db.execute('SELECT count(*) FROM learning_jobs WHERE space_id=?',(sid,)).fetchone()[0]
            rows = db.execute('''SELECT id,seq,thread_id,run_id,scope_kind,scope_id,state,reason,attempts,available,created,updated,summary
                FROM learning_jobs WHERE space_id=? ORDER BY seq DESC LIMIT 30 OFFSET ?''',(sid,(page-1)*30)).fetchall()
            counts = dict(db.execute('SELECT state,count(*) FROM learning_jobs WHERE space_id=? GROUP BY state',(sid,)))
            cursors = [dict(r) for r in db.execute('SELECT * FROM learning_cursors WHERE space_id=?',(sid,))]
        return {'items':[{**dict(r),'summary':json.loads(r['summary']),
                          'display_state':'paused' if (not active or not (cfg.get("enabled",True) and cfg['learn_memories'])) and r['state'] in ('pending','retry_wait','running') else r['state']} for r in rows],
                'total':total,'page':page,'counts':counts,'cursors':cursors,'paused':not active or not (cfg.get("enabled",True) and cfg['learn_memories'])}

    def action(self, sid, action, jid=None):
        self.memory.store.require_langmem(sid)
        with self.registry.guard(), self.store.connect() as db:
            self.registry.require_active(PROFILE)
            if action == 'flush':
                if not (self.store.config().get('enabled',True) and self.store.config()['learn_memories']):
                    raise ValueError('请先开启 LangMem 后台学习。')
                db.execute("UPDATE learning_jobs SET available=? WHERE space_id=? AND state IN ('pending','retry_wait')", (time.time(),sid))
                return {'ok':True}
            row = db.execute('SELECT * FROM learning_jobs WHERE id=? AND space_id=?',(jid,sid)).fetchone()
            if row is None: raise ValueError('整理任务不存在。')
            if action == 'cancel' and row['state'] not in TERMINAL:
                self.finish(db,row,'cancelled','user_cancelled')
            elif action == 'retry' and row['state'] in ('failed','retry_wait'):
                if self.store.epoch(sid) != row['source_epoch']:
                    self.finish(db,row,'skipped','manual_state_changed')
                    return {'ok':True,'status':'skipped'}
                cfg = self.store.config()
                if not (cfg.get("enabled",True) and cfg['learn_memories']): raise ValueError('请先开启 LangMem 后台学习。')
                frozen = json.loads(row['frozen'])
                frozen.update(cfg,activation_epoch=self.registry.state()['epoch'])
                db.execute("UPDATE learning_jobs SET state='pending',reason='',attempts=0,available=?,frozen=?,updated=? WHERE id=?",(time.time(),json.dumps(frozen),time.time(),jid))
            else:
                raise ValueError('任务状态已变化，请刷新后重试。')
        return {'ok':True}

    def claim(self, force=False):
        now = time.time()
        with self.registry.guard(), self.store.connect() as db:
            active = self.registry.state()
            cfg = self.store.config()
            if active['active_profile_id'] != PROFILE or not (cfg.get("enabled",True) and cfg['learn_memories']):
                return None
            db.execute('BEGIN IMMEDIATE')
            # A long lease exceeds bounded model requests. Tokens fence late owners.
            db.execute("UPDATE learning_jobs SET state='pending',lease=NULL,reason='lease_recovered' WHERE state='running' AND lease_until<?",(now,))
            row = db.execute('''SELECT * FROM learning_jobs j WHERE state IN ('pending','retry_wait') AND (available<=? OR ?)
                AND NOT EXISTS (SELECT 1 FROM learning_jobs p WHERE p.space_id=j.space_id AND p.thread_id=j.thread_id
                    AND p.seq<j.seq AND p.state NOT IN ('completed','skipped','cancelled')) ORDER BY seq LIMIT 1''',(now,int(force))).fetchone()
            if row is None: return None
            row = dict(row)
            frozen = json.loads(row['frozen'])
            reason = ('manual_state_changed' if self.store.epoch(row['space_id']) != row['source_epoch'] else
                      'config_changed' if frozen['revision'] != cfg['revision'] else '')
            if reason:
                self.finish(db,row,'skipped',reason)
                return {'skip':True}
            # Reactivation grants another attempt for the original authorized source,
            # never an attempt to ingest conversations from the other profile.
            frozen['activation_epoch'] = active['epoch']
            token = uuid4().hex
            db.execute("UPDATE learning_jobs SET state='running',lease=?,lease_until=?,attempts=attempts+1,updated=? WHERE id=?",(token,now+600,now,row['id']))
            return {**row,'lease':token,'attempts':row['attempts']+1,'frozen':frozen}

    def run_once(self, stop=None, force=False):
        job = self.claim(force)
        if not job: return False
        if job.get('skip'): return True
        frozen = job['frozen']
        try:
            proposals = self.memory.extract_candidates(frozen,job['source'])
            with self.registry.guard(), self.store.connect() as check:
                current = check.execute('SELECT * FROM learning_jobs WHERE id=?',(job['id'],)).fetchone()
                if current['state'] != 'running' or current['lease'] != job['lease']:
                    return True
                if not self.registry.valid(frozen) or (stop and stop.is_set()):
                    check.execute("UPDATE learning_jobs SET state='pending',lease=NULL,available=?,reason='profile_paused' WHERE id=?",(time.time(),job['id']))
                    return True
                if self.store.epoch(job['space_id']) != job['source_epoch']:
                    self.finish(check,job,'skipped','manual_state_changed'); return True
                if self.store.config()['revision'] != frozen['revision']:
                    self.finish(check,job,'skipped','config_changed'); return True
                # No writes on `check` before the formal commit on its own connection.
                def commit(db, summary):
                    row = db.execute('SELECT state,lease FROM learning_jobs WHERE id=?',(job['id'],)).fetchone()
                    if row['state'] != 'running' or row['lease'] != job['lease']:
                        raise ValueError('整理任务提交权已失效。')
                    self.finish(db,job,'completed','',summary)
                result = self.store.apply_langmem(job['space_id'],job['run_id'],job['thread_id'],job['source'],
                    revision=frozen['revision'],scope=(job['scope_kind'],job['scope_id']),epoch=job['source_epoch'],commit_hook=commit,**proposals)
                if result['status'] == 'skipped':
                    self.finish(check,job,'skipped','already_processed_or_invalid')
        except Exception:
            with self.registry.guard(), self.store.connect() as db:
                current = db.execute('SELECT state,lease FROM learning_jobs WHERE id=?',(job['id'],)).fetchone()
                if current['state'] == 'running' and current['lease'] == job['lease']:
                    state = 'failed' if job['attempts'] >= 3 else 'retry_wait'
                    db.execute('UPDATE learning_jobs SET state=?,reason=?,lease=NULL,available=?,updated=? WHERE id=?',
                               (state,'engine_failed',time.time()+min(300,15*2**(job['attempts']-1)),time.time(),job['id']))
        return True


class LearningWorker:
    def __init__(self, queue):
        self.queue = queue
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None

    def start(self):
        if self.thread: return
        self.thread = threading.Thread(target=self.loop,daemon=True,name='langmem-learning')
        self.thread.start()

    def loop(self):
        path = self.queue.registry.directory('langmem')/'learning-worker.lock'
        with path.open('a') as handle:
            while not self.stop.is_set():
                try:
                    fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    self.stop.wait(1)
            if self.stop.is_set(): return
            with self.queue.registry.guard(), self.queue.store.connect() as db:
                db.execute("UPDATE learning_jobs SET state='pending',lease=NULL,reason='worker_recovered' WHERE state='running'")
            with self.queue.registry.guard(), self.queue.store.connect() as db:
                db.execute("UPDATE episode_runs SET state='pending',lease=NULL,reason='worker_recovered' WHERE state='running'")
            with self.queue.registry.guard(), self.queue.store.connect() as db:
                db.execute("UPDATE rule_jobs SET state='pending',lease=NULL,reason='worker_recovered' WHERE state='running'")
            while not self.stop.is_set():
                try:
                    if self.queue.registry.state()['active_profile_id'] == PROFILE:
                        self.queue.recover_answers()
                        self.queue.episodes.recover()
                    did_work = self.queue.run_once(self.stop)
                    did_work = self.queue.episodes.run_once(self.stop) or did_work
                    did_work = self.queue.rules.run_once(self.stop) or did_work
                    if did_work: continue
                except Exception:
                    pass  # Durable rows remain recoverable; do not log model/source text.
                self.wake.wait(1); self.wake.clear()

    def close(self):
        self.stop.set(); self.wake.set()
        if self.thread: self.thread.join(timeout=1)
