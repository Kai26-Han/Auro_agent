"""Global background-memory policy and whole-space deletion boundaries."""
from contextvars import ContextVar
import sqlite3
import time
from .registry import PROFILES

INITIAL = {'langmem': 'personal', 'mem0': 'mem0-personal'}
CLEANUP_SPACE = ContextVar('memory_cleanup_space', default=None)


def managed(settings):
    path = settings.data_dir / 'memory-registry.sqlite'
    if not path.exists(): return False
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try: return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='workspace_spaces'").fetchone())
    finally: db.close()


def space_available(settings, sid):
    if CLEANUP_SPACE.get() == sid or not managed(settings): return True
    db = sqlite3.connect(f'file:{settings.data_dir}/memory-registry.sqlite?mode=ro', uri=True)
    try: return not db.execute('SELECT 1 FROM removed_memory_spaces WHERE id=?', (sid,)).fetchone()
    finally: db.close()


class WorkspaceMemory:
    def __init__(self, memory):
        self.memory = memory
        self.store = memory.store
        self.registry = self.store.registry

    def initialize(self):
        with self.registry.guard(), self.registry.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS workspace_spaces(engine TEXT PRIMARY KEY,space_id TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS removed_memory_spaces(id TEXT PRIMARY KEY,engine TEXT NOT NULL,name TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL)')
            for engine, sid in INITIAL.items():
                # Older installations may contain custom spaces but no seeded default.
                from .store import now
                with self.store.stores[engine].connect() as engine_db:
                    engine_db.execute('INSERT OR IGNORE INTO spaces VALUES (?,?,?,?)', (sid, '个人记忆', engine, now()))
                db.execute('INSERT OR IGNORE INTO workspace_spaces VALUES (?,?)', (engine, sid))

    def state(self):
        with self.registry.connect() as db:
            spaces = dict(db.execute('SELECT engine,space_id FROM workspace_spaces'))
            removed = {r['id']:dict(r) for r in db.execute('SELECT * FROM removed_memory_spaces')}
        active = self.registry.state()['active_profile_id']
        engine = self.registry.engine(active)
        return {'current_space_id': spaces[engine], 'engine_spaces': spaces, 'removed': removed}

    def selection(self):
        return {'space_id': self.state()['current_space_id'], 'scope_kind':'personal','scope_id':'personal'}

    def delete(self, sid):
        if sid in INITIAL.values(): raise ValueError('默认记忆空间不能删除。')
        # Read identity without reopening a space already fenced for deletion.
        matches=[]
        for engine, store in self.store.stores.items():
            with store.connect() as db:
                row=db.execute('SELECT * FROM spaces WHERE id=?',(sid,)).fetchone()
                if row: matches.append((engine,dict(row)))
        if len(matches)!=1: raise ValueError('记忆空间不存在。')
        engine, row=matches[0]
        self.registry.require_active(PROFILES[engine])
        if engine=='mem0':self.memory.native_mem0.store.require_no_batch(sid)
        token=CLEANUP_SPACE.set(sid)
        try:
            # Fence new reads/writes first. Failed physical cleanup can be retried.
            with self.registry.guard(),self.registry.connect() as db:
                db.execute('INSERT OR IGNORE INTO removed_memory_spaces VALUES (?,?,?,?,?)',(sid,engine,row['name'],'deleting',time.time()))
                db.execute('UPDATE workspace_spaces SET space_id=? WHERE engine=? AND space_id=?',(INITIAL[engine],engine,sid))
            if engine=='langmem':
                from .learning import LearningQueue
                LearningQueue(self.memory)
                lifecycle=self.memory.lifecycle
                with self.store.stores[engine].connect() as db: ids=list(lifecycle.objects(db,sid))
                for mid in ids:
                    with lifecycle.store.connect() as db: exists=mid in lifecycle.objects(db,sid)
                    if exists: lifecycle.delete(sid,mid)
                with lifecycle.store.connect() as db:
                    db.execute("UPDATE learning_jobs SET source='',state='cancelled',reason='space_deleted',lease=NULL WHERE space_id=?",(sid,))
                    db.execute("UPDATE episode_events SET text='',artifact=NULL WHERE job_id IN (SELECT id FROM episode_runs WHERE space_id=?)",(sid,))
                    db.execute("UPDATE episode_runs SET state='cancelled',reason='space_deleted',lease=NULL WHERE space_id=?",(sid,))
                    db.execute("UPDATE rule_jobs SET state='cancelled',reason='space_deleted',lease=NULL,result=NULL WHERE rule_id IN (SELECT id FROM collaboration_rules WHERE space_id=?)",(sid,))
                    db.execute('DELETE FROM episode_tracks WHERE space_id=?',(sid,))
                    db.execute('DELETE FROM lifecycle_contexts WHERE space_id=?',(sid,))
                    db.execute('DELETE FROM memory_profile_fields WHERE space_id=?',(sid,))
                    db.execute('DELETE FROM memory_profile_field_hidden WHERE space_id=?',(sid,))
                    lifecycle.store.touch_epoch(db,sid)
            else:
                native=self.memory.native_mem0
                for native in (native,native.channel('event'),native.channel('procedure')):
                    if not native.ready:continue
                    with native.store.connect() as db:
                        pending=[r[0] for r in db.execute("SELECT id FROM operations WHERE space_id=? AND state IN ('needs_reconcile','running')",(sid,))]
                    for oid in pending:
                        if native.reconcile(oid)['status'] not in ('completed','reconciled'): raise ValueError('原生删除待核对，请重试删除空间。')
                    with native.store.connect() as db:
                        ids=[dict(r) for r in db.execute("SELECT id,version FROM controls WHERE space_id=? AND state!='deleted'",(sid,))]
                    for item in ids:
                        if native.new_write(sid,'delete',mid=item['id'],version=item['version'])['status']!='completed':
                            raise ValueError('原生删除待核对，请重试删除空间。')
                    with native.store.connect() as db:
                        db.execute("UPDATE operations SET source='',state=CASE WHEN state IN ('pending','awaiting_answer','failed') THEN 'cancelled' ELSE state END WHERE space_id=?",(sid,))
                        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                        if 'contexts' in tables: db.execute('DELETE FROM contexts WHERE space_id=?',(sid,))
                        if native.channel_kind=='procedure':
                            db.execute('INSERT OR IGNORE INTO method_blocks SELECT space_id,key FROM methods WHERE space_id=?',(sid,))
                            db.execute("UPDATE methods SET state='deleted',title='',enabled=0,active_id=NULL,previous_id=NULL,candidate_id=NULL WHERE space_id=?",(sid,))
                with self.store.stores[engine].connect() as db:
                    db.execute("UPDATE memories SET deleted=1,status='deleted',content='',source_quote='' WHERE space_id=?",(sid,))
                    db.execute('UPDATE memory_changes SET before_value=NULL,after_value=NULL WHERE memory_id IN (SELECT id FROM memories WHERE space_id=?)',(sid,))
            with self.registry.guard(),self.registry.connect() as db:
                db.execute("UPDATE removed_memory_spaces SET state='deleted' WHERE id=?",(sid,))
            return {'deleted':True,'current_space_id':self.state()['current_space_id']}
        finally: CLEANUP_SPACE.reset(token)
