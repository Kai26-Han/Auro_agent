"""LangMem-owned retention, provenance and revocation barriers (no model calls).

Edges mean *possible influence*, never proof that an LLM relied on a source.
Revocation is atomic in the owned store; checkpoints are filtered before reuse.
"""
import hashlib
import json
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, Field

PROFILE = 'langmem-default'


def migrate(db):
    for sql in (
        'CREATE TABLE IF NOT EXISTS lifecycle_edges(source TEXT,target TEXT,kind TEXT,created REAL,PRIMARY KEY(source,target,kind))',
        'CREATE TABLE IF NOT EXISTS lifecycle_tombstones(id TEXT PRIMARY KEY,space_id TEXT,kind TEXT,created REAL)',
        'CREATE TABLE IF NOT EXISTS lifecycle_blocked_runs(space_id TEXT,thread_id TEXT,run_id TEXT,created REAL,PRIMARY KEY(space_id,thread_id,run_id))',
        'CREATE TABLE IF NOT EXISTS lifecycle_retention(id TEXT PRIMARY KEY,space_id TEXT,kind TEXT,expires REAL,updated REAL)',
        'CREATE TABLE IF NOT EXISTS lifecycle_events(id INTEGER PRIMARY KEY,space_id TEXT,kind TEXT,object_id TEXT,action TEXT,counts TEXT,created REAL)',
        'CREATE TABLE IF NOT EXISTS lifecycle_contexts(thread_id TEXT PRIMARY KEY,space_id TEXT,run_id TEXT,manifest TEXT,updated REAL)',
        'CREATE TABLE IF NOT EXISTS lifecycle_usage(id INTEGER PRIMARY KEY,kind TEXT,calls INTEGER,tokens INTEGER,unknown INTEGER,failed INTEGER,created REAL)',
        'CREATE TABLE IF NOT EXISTS index_generations(space_id TEXT,signature TEXT,state TEXT,total INTEGER,covered INTEGER,updated REAL,error TEXT,PRIMARY KEY(space_id,signature))',
    ): db.execute(sql)


def blocked(db, sid, thread, run):
    return bool(db.execute('SELECT 1 FROM lifecycle_blocked_runs WHERE space_id=? AND thread_id=? AND run_id=?', (sid, thread, run)).fetchone())


def unavailable(db, oid):
    return bool(db.execute('SELECT 1 FROM lifecycle_tombstones WHERE id=? UNION ALL SELECT 1 FROM lifecycle_retention WHERE id=? AND expires IS NOT NULL AND expires<=?', (oid,oid,time.time())).fetchone())


class RetentionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1)
    expires_at: datetime | None = None


class DeleteInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(min_length=64,max_length=64)


class Lifecycle:
    def __init__(self, memory):
        self.memory = memory
        self.registry = memory.store.registry
        self.store = memory.store.stores['langmem']
        # Ensure optional layer schemas exist in this engine only.
        self.episodes, self.rules = memory.episodes, memory.rules

    def objects(self, db, sid):
        result = {}
        for r in db.execute('SELECT id,memory_type,content,version,status,updated FROM memories WHERE space_id=? AND deleted=0',(sid,)):
            result[r['id']] = dict(id=r['id'],kind=r['memory_type'],title=r['content'][:100],version=r['version'],status=r['status'],updated=r['updated'])
        for r in db.execute("SELECT id,title,version,status,updated FROM episodes WHERE space_id=? AND status!='deleted'",(sid,)):
            result[r['id']] = {**dict(r),'kind':'episode'}
        for r in db.execute("SELECT id,coalesce((SELECT json_extract(data,'$.title') FROM rule_versions WHERE rule_id=collaboration_rules.id ORDER BY version DESC LIMIT 1),topic) AS title,revision AS version,state AS status,updated FROM collaboration_rules WHERE space_id=? AND state!='deleted'",(sid,)):
            result[r['id']] = {**dict(r),'kind':'rule'}
        return result

    def graph(self, db, sid):
        objects = self.objects(db,sid)
        edges, parents, turns, dates = defaultdict(set), defaultdict(set), {}, {}
        def edge(a,b):
            edges[a].add(b); parents[b].add(a)
        def turn(thread, run, created):
            if not thread or not run:return None
            key = 'turn:'+json.dumps([thread,run],ensure_ascii=False)
            turns[key] = (thread,run)
            value = datetime.fromisoformat(created).timestamp() if isinstance(created,str) else float(created)
            dates[key] = min(dates.get(key,value),value)
            return key
        for r in db.execute('SELECT source,target FROM lifecycle_edges'):edge(r['source'],r['target'])
        # Versioned provenance is intentionally retained across edits.
        for r in db.execute('SELECT s.* FROM memory_sources s JOIN memories m ON m.id=s.memory_id WHERE m.space_id=?',(sid,)):
            key=turn(r['source_thread'],r['source_run'],r['created'])
            if key:edge(key,r['memory_id'])
        for r in db.execute('SELECT id,source_thread,source_run,created FROM memories WHERE space_id=? AND deleted=0',(sid,)):
            key=turn(r['source_thread'],r['source_run'],r['created'])
            if key:edge(key,r['id'])
        for r in db.execute('SELECT * FROM episode_runs WHERE space_id=?',(sid,)):
            key=turn(r['thread_id'],r['run_id'],r['created'])
            if key:edge(key,r['task_id'])
        for r in db.execute("SELECT id,merged_into FROM episodes WHERE space_id=? AND merged_into IS NOT NULL",(sid,)):
            edge(r['id'],r['merged_into']);edge(r['merged_into'],r['id'])
        for r in db.execute('SELECT v.rule_id,v.sources FROM rule_versions v JOIN collaboration_rules r ON r.id=v.rule_id WHERE r.space_id=?',(sid,)):
            for source in json.loads(r['sources']):
                if source.get('kind')=='episode':edge(source['id'],r['rule_id'])
        for r in db.execute('SELECT * FROM memory_manifests WHERE space_id=? ORDER BY created,rowid',(sid,)):
            key=turn(r['thread_id'],r['run_id'],r['created'])
            for item in json.loads(r['items']):
                if key and item.get('included'):edge(item['id'],key)
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='learning_jobs'").fetchone():
            for r in db.execute('SELECT thread_id,run_id,created FROM learning_jobs WHERE space_id=?',(sid,)):turn(r['thread_id'],r['run_id'],r['created'])
        # Later turns may have seen an earlier answer or its running summary.
        threads=defaultdict(list)
        for key,(thread,_) in turns.items():threads[thread].append(key)
        for values in threads.values():
            ordered=sorted(values,key=lambda k:(dates[k],k))
            for a,b in zip(ordered,ordered[1:]):
                edge(a,b)
                if dates[a]==dates[b]:edge(b,a)
        return objects,edges,parents,turns

    def preview_in(self, db, sid, oid):
        objects,edges,parents,turns=self.graph(db,sid)
        if oid not in objects:raise ValueError('记忆对象不存在或已删除。')
        # Forget original turns as well, so replay cannot relearn their content.
        seeds={oid}|{p for p in parents[oid] if p in turns}
        seen=set(seeds); queue=deque(seeds)
        while queue:
            node=queue.popleft()
            neighbors=edges[node] | ({p for p in parents[node] if p in turns} if node not in turns else set())
            for child in neighbors:
                if child not in seen:seen.add(child);queue.append(child)
        affected=[objects[k] for k in sorted(seen) if k in objects]
        runs=[dict(thread_id=turns[k][0],run_id=turns[k][1]) for k in sorted(seen) if k in turns]
        token=hashlib.sha256(json.dumps([sid,oid,affected,runs],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        return {'root':oid,'items':affected,'runs':runs,'token':token,'counts':{kind:sum(r['kind']==kind for r in affected) for kind in ('profile','fact','episode','rule')},
                'relations':[{'source':a,'target':b} for a in sorted(seen) for b in sorted(edges[a]) if b in seen]}

    def preview(self,sid,oid):
        self.memory.store.require_langmem(sid)
        with self.store.connect() as db:return self.preview_in(db,sid,oid)

    def delete(self,sid,oid,token=None):
        self.memory.store.require_langmem(sid)
        with self.registry.guard(),self.store.connect() as db:
            self.registry.require_active(PROFILE);db.execute('BEGIN IMMEDIATE')
            report=self.preview_in(db,sid,oid)
            if token and token!=report['token']:raise ValueError('关联记忆已变化，请重新预览删除影响。')
            return self.delete_report_in(db,sid,oid,report)

    def delete_report_in(self,db,sid,oid,report):
        """Apply a freshly computed report inside the caller's guarded transaction."""
        from .store import now
        from .episode_schema import EpisodeDraft
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for run in report['runs']:
            args=(sid,run['thread_id'],run['run_id'])
            db.execute('INSERT OR IGNORE INTO lifecycle_blocked_runs VALUES (?,?,?,?)',(*args,time.time()))
            if 'learning_jobs' in tables:
                db.execute("UPDATE learning_jobs SET source='',state='cancelled',reason='source_deleted',lease=NULL WHERE space_id=? AND thread_id=? AND run_id=?",args)
            db.execute("UPDATE episode_events SET text='',artifact=NULL WHERE job_id IN (SELECT id FROM episode_runs WHERE space_id=? AND thread_id=? AND run_id=?)",args)
            db.execute("UPDATE episode_runs SET state='cancelled',reason='source_deleted',lease=NULL WHERE space_id=? AND thread_id=? AND run_id=?",args)
            db.execute("UPDATE memory_reviews SET proposal=NULL,status='deleted' WHERE space_id=? AND source_thread=? AND source_run=?",args)
        for item in report['items']:
            mid=item['id'];kind=item['kind']
            db.execute('INSERT OR IGNORE INTO lifecycle_tombstones VALUES (?,?,?,?)',(mid,sid,kind,time.time()))
            if kind in ('fact','profile'):
                row=db.execute('SELECT * FROM memories WHERE id=?',(mid,)).fetchone()
                db.execute('INSERT OR IGNORE INTO blocked_hashes VALUES (?,?)',(sid,row['fingerprint']))
                if kind=='profile':db.execute('INSERT OR IGNORE INTO memory_blocked_topics VALUES (?,?)',(sid,row['profile_key']))
                db.execute("UPDATE memories SET deleted=1,status='deleted',content='',source_quote='',conditions='',topic_key='',version=version+1,updated=? WHERE id=?",(now(),mid))
                db.execute('UPDATE memory_changes SET before_value=NULL,after_value=NULL WHERE memory_id=?',(mid,))
                db.execute('DELETE FROM memory_sources WHERE memory_id=?',(mid,))
                db.execute('DELETE FROM memory_vectors WHERE memory_id=?',(mid,))
                db.execute("UPDATE memory_reviews SET proposal=NULL,status='deleted' WHERE target_id=?",(mid,))
                db.execute("INSERT INTO events(memory_id,action,created) VALUES (?,'delete',?)",(mid,now()))
            elif kind=='episode':
                empty=EpisodeDraft(title='已删除',context='').model_dump_json()
                db.execute("UPDATE episodes SET title='',goal='',body=?,outcome='unknown',basis='unconfirmed',evidence_id=NULL,status='deleted',version=version+1,updated=? WHERE id=?",(empty,time.time(),mid))
                db.execute('DELETE FROM episode_vectors WHERE episode_id=?',(mid,))
                db.execute("UPDATE episode_events SET text='',artifact=NULL WHERE job_id IN (SELECT id FROM episode_runs WHERE task_id=?)",(mid,))
                db.execute("UPDATE episode_changes SET reason='' WHERE episode_id=?",(mid,))
                db.execute("UPDATE episode_runs SET state='cancelled',lease=NULL,reason='source_deleted' WHERE task_id=?",(mid,))
                db.execute('DELETE FROM episode_tracks WHERE task_id=?',(mid,))
            else:
                for v in db.execute('SELECT sources FROM rule_versions WHERE rule_id=?',(mid,)):
                    for source in json.loads(v['sources']):db.execute('INSERT OR IGNORE INTO rule_suppressions VALUES (?,?)',(mid,source['hash']))
                db.execute("UPDATE collaboration_rules SET state='deleted',topic=topic||':deleted:'||id,active_version=NULL,revision=revision+1,updated=? WHERE id=?",(time.time(),mid))
                db.execute("UPDATE rule_versions SET sources='[]',prompt='',data='{}' WHERE rule_id=?",(mid,))
                db.execute("UPDATE rule_jobs SET result=NULL,state='cancelled',lease=NULL,reason='source_deleted' WHERE rule_id=?",(mid,))
                db.execute('DELETE FROM rule_vectors WHERE rule_id=?',(mid,))
        self.store.touch_epoch(db,sid)
        db.execute("UPDATE index_generations SET state='stale' WHERE space_id=?",(sid,))
        db.execute("INSERT INTO lifecycle_events(space_id,kind,object_id,action,counts,created) VALUES (?,?,?,'delete',?,?)",(sid,'cascade',oid,json.dumps(report['counts']),time.time()))
        return {'deleted':True,'counts':report['counts'],'contexts':len({r['thread_id'] for r in report['runs']})}

    def delete_memories_in(self, db, sid, ids, object_id='manual-selection'):
        """Delete only explicitly selected semantic records.

        Category/profile editors are direct data management surfaces. A
        provenance edge there means possible historical influence, not that the
        user selected every downstream object for deletion.
        """
        from .store import now
        rows=[]
        for mid in dict.fromkeys(ids):
            row=db.execute("SELECT * FROM memories WHERE id=? AND space_id=? AND deleted=0",(mid,sid)).fetchone()
            if not row:raise ValueError('记忆已变化，请刷新后重试。')
            rows.append(row)
        for row in rows:
            mid=row['id']
            db.execute('INSERT OR IGNORE INTO lifecycle_tombstones VALUES (?,?,?,?)',(mid,sid,row['memory_type'],time.time()))
            db.execute('INSERT OR IGNORE INTO blocked_hashes VALUES (?,?)',(sid,row['fingerprint']))
            if row['memory_type']=='profile':
                db.execute('INSERT OR IGNORE INTO memory_blocked_topics VALUES (?,?)',(sid,row['profile_key']))
            db.execute("UPDATE memories SET deleted=1,status='deleted',content='',source_quote='',conditions='',topic_key='',version=version+1,updated=? WHERE id=?",(now(),mid))
            db.execute('UPDATE memory_changes SET before_value=NULL,after_value=NULL WHERE memory_id=?',(mid,))
            db.execute('DELETE FROM memory_sources WHERE memory_id=?',(mid,))
            db.execute('DELETE FROM memory_vectors WHERE memory_id=?',(mid,))
            db.execute("UPDATE memory_reviews SET proposal=NULL,status='deleted' WHERE target_id=?",(mid,))
            db.execute("INSERT INTO events(memory_id,action,created) VALUES (?,'delete',?)",(mid,now()))
        if rows:
            self.store.touch_epoch(db,sid)
            counts={'profile':sum(r['memory_type']=='profile' for r in rows),'fact':sum(r['memory_type']=='fact' for r in rows),'episode':0,'rule':0}
            db.execute("INSERT INTO lifecycle_events(space_id,kind,object_id,action,counts,created) VALUES (?,?,?,'delete',?,?)",(sid,'direct',object_id,json.dumps(counts),time.time()))
        else:counts={'profile':0,'fact':0,'episode':0,'rule':0}
        return {'deleted':True,'counts':counts,'contexts':0}

    def delete_memory_only(self, sid, oid):
        self.memory.store.require_langmem(sid)
        with self.registry.guard(),self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return self.delete_memories_in(db,sid,[oid],oid)

    def retention(self,sid,oid,body):
        self.memory.store.require_langmem(sid)
        expires=body.expires_at
        if expires and expires.tzinfo is None:raise ValueError('过期时间必须包含时区。')
        with self.registry.guard(),self.store.connect() as db:
            self.registry.require_active(PROFILE)
            item=self.objects(db,sid).get(oid)
            if not item or item['version']!=body.version:raise ValueError('记忆版本已变化，请刷新后重试。')
            db.execute('INSERT INTO lifecycle_retention VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET expires=excluded.expires,updated=excluded.updated',(oid,sid,item['kind'],expires.timestamp() if expires else None,time.time()))
            self.store.touch_epoch(db,sid)
            db.execute("INSERT INTO lifecycle_events(space_id,kind,object_id,action,counts,created) VALUES (?,?,?,'retention','{}',?)",(sid,item['kind'],oid,time.time()))
        return {'saved':True}

    def sanitize(self,thread,state):
        """Fail closed before model/tool continuation; raw messages remain viewable."""
        with self.store.connect() as db:
            records=db.execute('SELECT run_id,created FROM lifecycle_blocked_runs WHERE thread_id=?',(thread,)).fetchall()
            runs={r['run_id'] for r in records}
            barrier=max((r['created'] for r in records),default=0)
        excluded=set(state.get('working_excluded_turns',[]))
        for m in state.get('messages',[]):
            if m.type=='human' and runs and (m.additional_kwargs.get('run_id') in runs or m.additional_kwargs.get('memory_created_at',0)<barrier):excluded.add(m.id)
        marker=hashlib.sha256(json.dumps(sorted(runs)).encode()).hexdigest()
        if not runs or (state.get('memory_revocation')==marker and excluded==set(state.get('working_excluded_turns',[]))):return {}
        return {'working_excluded_turns':sorted(excluded),'memory_revocation':marker,
                'running_summary':{'version':state.get('running_summary',{}).get('version',0)+1},'request_summary':{},
                'context_compression':{},
                'task_state':{'version':state.get('task_state',{}).get('version',0)+1},
                'prepared_messages':[],'context_budget':{},'context_error':'',
                'context_notice':'','context_diagnostic':{},'memory_contexts':{}}

    def context_manifest(self,thread,sid,state):
        summary=state.get('running_summary',{});task=state.get('task_state',{})
        compression=state.get('context_compression',{})
        manifest={'summary_version':summary.get('version',0),'summary_sources':summary.get('source_ids',[]),
                  'compression_schema':compression.get('schema_version'),
                  'compression_segments':len(compression.get('segments',[])),
                  'compression_core':bool(compression.get('core')),
                  'task_version':task.get('version',0),'task_sources':task.get('source_ids',[]),
                  'excluded_turns':state.get('working_excluded_turns',[]),'revocation':state.get('memory_revocation'),
                  'budget':state.get('context_budget',{}),'usage':state.get('context_usage',{}),
                  'meaning':'prepared_not_proven_used'}
        with self.registry.guard(),self.store.connect() as db:
            if self.registry.state()['active_profile_id']!=PROFILE:return
            db.execute('INSERT INTO lifecycle_contexts VALUES (?,?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET space_id=excluded.space_id,run_id=excluded.run_id,manifest=excluded.manifest,updated=excluded.updated',(thread,sid,state.get('turn_id',''),json.dumps(manifest),time.time()))

    def dashboard(self,sid,page=1,kind='all'):
        self.memory.store.require_langmem(sid)
        with self.store.connect() as db:
            objects=self.objects(db,sid)
            policies={r['id']:dict(r) for r in db.execute('SELECT * FROM lifecycle_retention WHERE space_id=?',(sid,))}
            rows=[]
            for item in objects.values():
                expiry=policies.get(item['id'],{}).get('expires')
                updated=item['updated']; updated=datetime.fromisoformat(updated).timestamp() if isinstance(updated,str) else updated
                rows.append({**item,'expires':expiry,'expired':expiry is not None and expiry<=time.time(),'archive_suggested':item['status']=='active' and updated<time.time()-180*86400})
            rows=sorted((r for r in rows if kind=='all' or r['kind']==kind),key=lambda r:(r['kind'],r['id']))
            contexts=[{**dict(r),'manifest':json.loads(r['manifest'])} for r in db.execute('SELECT * FROM lifecycle_contexts WHERE space_id=? ORDER BY updated DESC LIMIT 30 OFFSET ?',(sid,(page-1)*30))]
            for c in contexts:
                runs=sorted(r[0] for r in db.execute('SELECT run_id FROM lifecycle_blocked_runs WHERE thread_id=?',(c['thread_id'],)))
                c['needs_rebuild']=bool(runs) and c['manifest'].get('revocation')!=hashlib.sha256(json.dumps(runs).encode()).hexdigest()
            usage=[dict(r) for r in db.execute('SELECT kind,sum(calls) AS calls,sum(tokens) AS tokens,sum(unknown) AS unknown,sum(failed) AS failed FROM lifecycle_usage GROUP BY kind')]
            indices=[dict(r) for r in db.execute('SELECT * FROM index_generations WHERE space_id=?',(sid,))]
            active={r['id']:hashlib.sha256(r['content'].encode()).hexdigest() for r in db.execute("SELECT id,content FROM memories WHERE space_id=? AND memory_type='fact' AND status='active' AND deleted=0",(sid,)) if not unavailable(db,r['id'])}
            for index in indices:
                cache={r['memory_id']:r['fingerprint'] for r in db.execute('SELECT memory_id,fingerprint FROM memory_vectors WHERE space_id=? AND signature=?',(sid,index['signature']))}
                index['total']=len(active);index['covered']=sum(cache.get(mid)==digest for mid,digest in active.items())
                if index['state']=='ready' and index['covered']!=index['total']:index['state']='stale'

            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            jobs={name:dict(db.execute(f'SELECT state,count(*) FROM {table} WHERE space_id=? GROUP BY state',(sid,))) for name,table in [('semantic','learning_jobs'),('episode','episode_runs')] if table in tables}
            jobs['rule']=dict(db.execute('SELECT j.state,count(*) FROM rule_jobs j JOIN collaboration_rules r ON r.id=j.rule_id WHERE r.space_id=? GROUP BY j.state',(sid,)))
            audit=[{**dict(r),'counts':json.loads(r['counts'])} for r in db.execute('SELECT * FROM lifecycle_events WHERE space_id=? ORDER BY id DESC LIMIT 30 OFFSET ?',(sid,(page-1)*30))]
            pending=db.execute("SELECT count(*) FROM memory_reviews WHERE space_id=? AND status='pending'",(sid,)).fetchone()[0]
        return {'items':rows[(page-1)*30:page*30],'total':len(rows),'page':page,'contexts':contexts,'indices':indices,'usage':usage,'jobs':jobs,'audit':audit,'pending_reviews':pending,'coverage_limit':None}
