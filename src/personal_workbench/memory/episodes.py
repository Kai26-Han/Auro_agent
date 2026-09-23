"""LangMem-owned event ledger, episodic queue, governance and case selection.

Only live authorized turns are observed. Model drafts cannot decide outcomes,
forge sources, create artifact URLs or write semantic/procedural memories.
"""
import hashlib
import json
import math
import re
import sqlite3
import time
from uuid import uuid4
from pathlib import PurePosixPath
from .episode_schema import EpisodeDraft
from .foundation import scope_of, PERSONAL
from .learning import EXCLUDED

PROFILE='langmem-default'
MEMORY_TOOLS={'search_memory','manage_memory'}
TERMINAL={'completed','skipped','cancelled'}

def encode(value): return json.dumps(value,ensure_ascii=False)
def feedback(text):
    # Explicit first-person/current-task feedback, not quoted tool or assistant text.
    text=text.strip()
    if len(text)>600 or re.search(r'[?？]|如果|假如|假设|文档|他说|她说|引文|假定|if |would ',text,re.I): return None
    text=re.sub(r'^(?:谢谢[，,！!。 ]*|这次|现在|我确认[：:，, ]*|结果[是：: ]*)','',text)
    for outcome,pattern in [('failure',r'^(?:还是|仍然|我)?(?:没有解决|没解决|未解决|失败了|没有成功|没成功|没有跑通|没跑通|还没理解|failed\b|did not work\b)'),('partial',r'^(?:部分解决|只解决了|部分成功|partly worked\b)'),('success',r'^(?:我)?(?:已经)?(?:解决了|成功了|跑通了|明白了|理解了|任务完成了|it worked\b|it works\b|solved\b)')]:
        if re.search(pattern,text,re.I): return outcome
    return None

def tokens(text):
    return set(re.findall(r'[a-z0-9_]{2,}|[\u4e00-\u9fff]{2}',text.lower())) | {text[i:i+2] for i in range(len(text)-1) if all('\u4e00'<=c<='\u9fff' for c in text[i:i+2])}

def recall_intent(query):
    """Distinguish reconstructing history from reusing a proven approach."""
    return 'history' if re.search(r'上次|之前|以前|历史|回顾|做过|发生过|做到哪|进展|当时|哪次|什么时候|last time|previously|history|progress',query,re.I) else 'guidance'

def search_text(row,body):
    lessons=' '.join(note.get('text','') for note in body.get('lessons',[]))
    return ' '.join(filter(None,[row['title'],row['task_type'],row['goal'],body.get('context',''),body.get('applicability',''),lessons]))

class Episodes:
    def __init__(self,memory):
        self.memory=memory; self.registry=memory.store.registry; self.store=memory.store.stores['langmem']
        with self.store.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS episode_tracks(space_id TEXT,thread_id TEXT,scope_kind TEXT,scope_id TEXT,task_id TEXT NOT NULL,PRIMARY KEY(space_id,thread_id,scope_kind,scope_id));
            CREATE TABLE IF NOT EXISTS episode_runs(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,space_id TEXT NOT NULL,thread_id TEXT NOT NULL,run_id TEXT NOT NULL,scope_kind TEXT NOT NULL,scope_id TEXT NOT NULL,frozen TEXT NOT NULL,epoch INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',attempts INTEGER NOT NULL DEFAULT 0,available REAL NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,lease TEXT,lease_until REAL,manual INTEGER NOT NULL DEFAULT 0,UNIQUE(space_id,thread_id,run_id));
            CREATE TABLE IF NOT EXISTS episode_events(id TEXT PRIMARY KEY,job_id TEXT NOT NULL,role TEXT NOT NULL,kind TEXT NOT NULL,text TEXT NOT NULL,tool TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT '',artifact TEXT,created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS episode_events_job ON episode_events(job_id,created);
            CREATE INDEX IF NOT EXISTS episode_runs_due ON episode_runs(state,available,created);
            CREATE TABLE IF NOT EXISTS episodes(id TEXT PRIMARY KEY,space_id TEXT NOT NULL,scope_kind TEXT NOT NULL,scope_id TEXT NOT NULL,title TEXT NOT NULL,task_type TEXT NOT NULL,goal TEXT NOT NULL,body TEXT NOT NULL,outcome TEXT NOT NULL,basis TEXT NOT NULL,evidence_id TEXT,version INTEGER NOT NULL DEFAULT 1,status TEXT NOT NULL DEFAULT 'active',locked INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL,merged_into TEXT);
            CREATE TABLE IF NOT EXISTS episode_changes(id INTEGER PRIMARY KEY AUTOINCREMENT,episode_id TEXT NOT NULL,action TEXT NOT NULL,version INTEGER NOT NULL,reason TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS episode_vectors(space_id TEXT NOT NULL,episode_id TEXT NOT NULL,version INTEGER NOT NULL,signature TEXT NOT NULL,fingerprint TEXT NOT NULL,vector TEXT NOT NULL,PRIMARY KEY(space_id,episode_id,signature));
            ''')

    def allowed(self,frozen,manual=False):
        if not frozen or frozen.get('engine')!='langmem' or not frozen.get('enabled',True) or not self.registry.valid(frozen): return False
        cfg=self.store.config()
        return cfg['revision']==frozen['revision'] and (manual or ((cfg.get("enabled",True) and cfg['learn_memories']) and frozen.get('learn_memories') and cfg['langmem']['episodes']))

    def begin(self,frozen,text,thread,run,manual=False):
        with self.registry.guard(),self.store.connect() as db:
            if not self.allowed(frozen,manual) or EXCLUDED.search(text): return None
            from .lifecycle import blocked
            if blocked(db,frozen['space_id'],thread,run):return None
            sid=frozen['space_id']; scope=scope_of(frozen); self.store.validate_scope(sid,scope)
            prior=db.execute('SELECT id FROM episode_runs WHERE space_id=? AND thread_id=? AND run_id=?',(sid,thread,run)).fetchone()
            if prior:return prior['id']
            track=db.execute('SELECT task_id FROM episode_tracks WHERE space_id=? AND thread_id=? AND scope_kind=? AND scope_id=?',(sid,thread,*scope)).fetchone()
            task=track['task_id'] if track else uuid4().hex
            if not track:db.execute('INSERT INTO episode_tracks VALUES (?,?,?,?,?)',(sid,thread,*scope,task))
            now=time.time(); jid=uuid4().hex
            db.execute('''INSERT INTO episode_runs(id,task_id,space_id,thread_id,run_id,scope_kind,scope_id,frozen,epoch,state,available,created,updated,manual) VALUES (?,?,?,?,?,?,?,?,?,'collecting',?,?,?,?)''',(jid,task,sid,thread,run,*scope,encode(frozen),self.store.epoch(sid),now,now,now,int(manual)))
            self.event(db,jid,'user:'+run,'user','request',text,status=feedback(text) or '')
            return jid

    def event(self,db,jid,key,role,kind,text,tool='',status='',artifact=None):
        if EXCLUDED.search(text):text='[此来源含不记录要求或敏感标记，正文未保留]';status=''
        eid=hashlib.sha256((jid+':'+key).encode()).hexdigest()
        db.execute('INSERT OR IGNORE INTO episode_events VALUES (?,?,?,?,?,?,?,?,?)',(eid,jid,role,kind,text[:4000]+('\n[内容已节选，完整原文见来源对话]' if len(text)>4000 else ''),tool,status,encode(artifact) if artifact else None,time.time()))

    def observe(self,jid,messages):
        if not jid:return
        with self.registry.guard(),self.store.connect() as db:
            row=db.execute('SELECT * FROM episode_runs WHERE id=?',(jid,)).fetchone()
            if not row or row['state']!='collecting' or not self.allowed(json.loads(row['frozen']),row['manual']):return
            for m in messages:
                if m.type=='ai' and not getattr(m,'tool_calls',[]):
                    self.event(db,jid,m.id or hashlib.sha256(m.text.encode()).hexdigest(),'assistant','answer',m.text[:2000]+('\n[内容已节选，完整原文见来源对话]' if len(m.text)>2000 else ''))
                if m.type!='tool' or getattr(m,'name','') in MEMORY_TOOLS:continue
                name=getattr(m,'name','') or 'tool'; artifact=None
                try:result=json.loads(m.text)
                except (ValueError,TypeError):result={}
                if not isinstance(result,dict):result={}
                error=m.status=='error' or bool(result.get('error') or result.get('is_error') or result.get('isError'))
                status='failure' if error else 'returned'
                # Preserve only the receipt, never untrusted webpage/file/MCP bodies.
                text='工具返回错误；不把此步骤视为成功。' if error else '工具返回结果；这不证明整个任务已成功。'
                if name in {'create_note','create_artifact'} and not error and isinstance(result.get('saved'),str):
                    filename=result['saved']
                    if PurePosixPath(filename).name==filename and '\\' not in filename:
                        try:
                            from personal_workbench.workspace import ARTIFACT_SUFFIXES, MAX_ARTIFACT_BYTES, read_bytes
                            data=read_bytes(self.memory.settings.outputs_dir/row['thread_id'],filename,ARTIFACT_SUFFIXES,MAX_ARTIFACT_BYTES)
                            artifact={'thread_id':row['thread_id'],'name':filename,'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)}
                            status='artifact_verified';text='已核对本机保存产物的存在、大小与 SHA-256；尚未验证内容质量或任务整体结果。'
                        except (ValueError,OSError):status='unverified';text='工具报告保存，但未能核对本机产物。'
                self.event(db,jid,'tool:'+m.tool_call_id,'tool','action',text,name,status,artifact)

    def finish(self,jid,status='completed',explicit=False):
        if not jid:return
        with self.registry.guard(),self.store.connect() as db:
            row=db.execute('SELECT * FROM episode_runs WHERE id=?',(jid,)).fetchone()
            if not row or row['state'] not in ('collecting','idle'):return
            if status in ('running','pending','waiting_approval','interrupted','stopped','failed'):return
            if explicit:self.event(db,jid,'manual-save','user','save_request','用户明确保存当前任务经历。')
            # A pre-existing episode is continued; an ordinary answer alone is idle.
            trigger=explicit or db.execute("SELECT 1 FROM episode_events WHERE job_id=? AND (role='tool' OR (role='user' AND status!=''))",(jid,)).fetchone() or db.execute('SELECT 1 FROM episodes WHERE id=?',(row['task_id'],)).fetchone()
            state='pending' if trigger else 'idle'
            cfg=json.loads(row['frozen'])
            delay=0 if explicit else cfg['langmem']['debounce_seconds']
            db.execute('UPDATE episode_runs SET state=?,available=?,updated=? WHERE id=?',(state,time.time()+delay,time.time(),jid))
            # Retain only the immediately preceding idle turn as optional context.
            old=db.execute("SELECT id FROM episode_runs WHERE task_id=? AND state='idle' ORDER BY created DESC LIMIT -1 OFFSET 1",(row['task_id'],)).fetchall()
            for item in old:
                db.execute('DELETE FROM episode_events WHERE job_id=?',(item['id'],))
                db.execute("UPDATE episode_runs SET state='skipped',reason='idle_superseded' WHERE id=?",(item['id'],))

    def cancel(self,jid,reason='parent_not_accepted'):
        """Discard a pre-registered parent source that was not accepted."""
        if not jid:return
        with self.registry.guard(),self.store.connect() as db:
            row=db.execute('SELECT state FROM episode_runs WHERE id=?',(jid,)).fetchone()
            if not row or row['state'] not in ('collecting','idle'):return
            db.execute('DELETE FROM episode_events WHERE job_id=?',(jid,))
            db.execute("UPDATE episode_runs SET state='cancelled',reason=?,updated=? WHERE id=?",(reason,time.time(),jid))

    def recover(self):
        with self.store.connect() as db:rows=db.execute("SELECT id,thread_id,run_id FROM episode_runs WHERE state='collecting'").fetchall()
        from personal_workbench.workflows.memory import parent_outcome,delivery_receipt
        path=self.registry.root/'web.sqlite'
        for row in rows:
            parent=parent_outcome(self.registry.root,row['thread_id'],row['run_id'])
            if parent and parent['terminal']:
                if parent['accepted']:self.observe(row['id'],delivery_receipt(parent['artifact']))
                if parent['accepted']:self.finish(row['id'],'completed')
                else:self.cancel(row['id'],'parent_not_accepted')
                continue
            if path.exists():
                with sqlite3.connect(f'file:{path}?mode=ro',uri=True) as db:
                    job=db.execute("SELECT status FROM jobs WHERE thread_id=? AND json_extract(snapshot,'$.run_id')=? ORDER BY rowid DESC LIMIT 1",(row['thread_id'],row['run_id'])).fetchone()
                if job and job[0] not in ('queued','running','stopping','pending','interrupted'):self.finish(row['id'],job[0])

    def events(self,db,task,limit=60,before=1e30,epoch=0,revision=0):
        return [dict(r) for r in db.execute('''SELECT e.*,r.thread_id,r.run_id FROM episode_events e JOIN episode_runs r ON r.id=e.job_id WHERE r.task_id=? AND r.state NOT IN ('skipped','cancelled','collecting') AND r.created<=? AND (r.state='completed' OR (r.epoch=? AND json_extract(r.frozen,'$.revision')=?)) ORDER BY e.created DESC,e.rowid DESC LIMIT ?''',(task,before,epoch,revision,limit))][::-1]

    @staticmethod
    def outcome(events):
        feedbacks=[e for e in events if e['role']=='user' and e['status'] in ('success','partial','failure','unknown')]
        latest_action=max((e['created'] for e in events if e['role']=='tool'),default=0)
        if feedbacks and feedbacks[-1]['created']>=latest_action:return feedbacks[-1]['status'],'user_feedback',feedbacks[-1]['id']
        errors=[e for e in events if e['role']=='tool' and e['status']=='failure']
        artifacts=[e for e in events if e['artifact']]
        if artifacts:return 'partial','artifact_verified',artifacts[-1]['id']
        if errors:return 'failure','tool_step_failed',errors[-1]['id']
        return 'unknown','unconfirmed',None

    def run_once(self,stop=None,force=False):
        with self.registry.guard(),self.store.connect() as db:
            if self.registry.state()['active_profile_id']!=PROFILE:return False
            cfg=self.store.config();now=time.time()
            db.execute("UPDATE episode_runs SET state='pending',lease=NULL WHERE state='running' AND lease_until<?",(now,))
            row=db.execute("""SELECT * FROM episode_runs r WHERE state IN ('pending','retry_wait') AND (available<=? OR ?) AND (manual=1 OR ?) AND NOT EXISTS(SELECT 1 FROM episode_runs p WHERE p.task_id=r.task_id AND p.created<r.created AND p.state IN ('collecting','pending','running','retry_wait','failed')) ORDER BY created LIMIT 1""",(now,int(force),int((cfg.get("enabled",True) and cfg['learn_memories']) and cfg['langmem']['episodes']))).fetchone()
            if not row:return False
            row=dict(row); frozen=json.loads(row['frozen']);frozen['activation_epoch']=self.registry.state()['epoch']
            previous=db.execute('SELECT * FROM episodes WHERE id=?',(row['task_id'],)).fetchone()
            reason=('manual_state_changed' if row['epoch']!=self.store.epoch(row['space_id']) else 'config_changed' if frozen['revision']!=cfg['revision'] else 'case_protected' if previous and (previous['status']!='active' or previous['locked']) else '')
            if reason:
                db.execute("UPDATE episode_runs SET state='skipped',reason=? WHERE id=?",(reason,row['id']));return True
            lease=uuid4().hex
            db.execute("UPDATE episode_runs SET state='running',attempts=attempts+1,lease=?,lease_until=? WHERE id=?",(lease,now+600,row['id']))
            events=self.events(db,row['task_id'],before=row['created'],epoch=row['epoch'],revision=frozen['revision']); previous=dict(previous) if previous else None
            evidence=[dict(e) for e in db.execute("""SELECT e.id,e.role,e.status,e.artifact,e.created FROM episode_events e JOIN episode_runs r ON r.id=e.job_id WHERE r.task_id=? AND r.created<=? AND r.state NOT IN ('skipped','cancelled','collecting') AND (r.state='completed' OR (r.epoch=? AND json_extract(r.frozen,'$.revision')=?)) AND (e.role='tool' OR (e.role='user' AND e.status!='')) ORDER BY e.created,e.rowid""",(row['task_id'],row['created'],row['epoch'],frozen['revision']))]
        try:
            # Bound native manager input; references are rechecked on commit.
            sources=[];used=0
            for e in reversed(events):
                source={k:e[k] for k in ('id','role','kind','text','tool','status')}
                cost=len(encode(source))
                if used+cost>16000:break
                sources.append(source);used+=cost
            sources.reverse()
            draft=EpisodeDraft.model_validate(self.memory.engine(frozen).extract_episode(sources, json.loads(previous['body']) if previous else None))
            byid={e['id']:e for e in sources}
            draft.lessons=[n for n in draft.lessons if n.source_id in byid and n.quote in byid[n.source_id]['text'] and byid[n.source_id]['role']!='assistant']
            with self.registry.guard(),self.store.connect() as db:
                live=db.execute('SELECT * FROM episode_runs WHERE id=?',(row['id'],)).fetchone()
                if live['state']!='running' or live['lease']!=lease:return True
                if not self.registry.valid(frozen) or (stop and stop.is_set()):
                    db.execute("UPDATE episode_runs SET state='pending',lease=NULL,reason='profile_paused' WHERE id=?",(row['id'],));return True
                current=db.execute('SELECT * FROM episodes WHERE id=?',(row['task_id'],)).fetchone()
                if self.store.epoch(row['space_id'])!=row['epoch'] or self.store.config()['revision']!=frozen['revision'] or (current and (current['locked'] or current['status']!='active')):
                    db.execute("UPDATE episode_runs SET state='skipped',reason='manual_or_config_changed' WHERE id=?",(row['id'],));return True
                outcome,basis,evidence_id=self.outcome(evidence)
                if current and current['basis']=='user_correction':
                    outcome,basis,evidence_id=current['outcome'],current['basis'],current['evidence_id']
                goal=previous['goal'] if previous else next((e['text'][:600] for e in events if e['kind']=='request'),'任务经历')
                version=(current['version']+1) if current else 1
                db.execute('''INSERT INTO episodes(id,space_id,scope_kind,scope_id,title,task_type,goal,body,outcome,basis,evidence_id,version,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,task_type=excluded.task_type,body=excluded.body,outcome=excluded.outcome,basis=excluded.basis,evidence_id=excluded.evidence_id,version=excluded.version,updated=excluded.updated''',(row['task_id'],row['space_id'],row['scope_kind'],row['scope_id'],draft.title,draft.task_type,goal,encode(draft.model_dump()),outcome,basis,evidence_id,version,previous['created'] if previous else now,time.time()))
                db.execute('DELETE FROM episode_vectors WHERE episode_id=?',(row['task_id'],))
                db.execute('INSERT INTO episode_changes(episode_id,action,version,reason,created) VALUES (?,?,?,?,?)',(row['task_id'],'consolidated',version,'',time.time()))
                db.execute("UPDATE episode_runs SET state='completed',reason='',lease=NULL,updated=? WHERE id=?",(time.time(),row['id']))
        except Exception:
            with self.registry.guard(),self.store.connect() as db:
                db.execute("UPDATE episode_runs SET state=CASE WHEN attempts>=3 THEN 'failed' ELSE 'retry_wait' END,reason='engine_failed',available=?,lease=NULL WHERE id=? AND state='running' AND lease=?",(time.time()+15*2**row['attempts'],row['id'],lease))
        return True

    def public_row(self,row):
        return {**dict(row),'body':json.loads(row['body']),'locked':bool(row['locked'])}

    def get(self,eid):
        with self.store.connect() as db:row=db.execute('SELECT * FROM episodes WHERE id=?',(eid,)).fetchone()
        if not row:raise ValueError('任务经历不存在。')
        return self.public_row(row)

    def listing(self,sid,scope=PERSONAL,q='',status='active',outcome='',page=1):
        self.memory.store.require_langmem(sid);self.store.validate_scope(sid,scope)
        if status not in ('active','archived','merged','all'):raise ValueError('无效的经历状态。')
        where="space_id=? AND (scope_kind=? AND scope_id=? OR ?) AND status!='deleted'";args=[sid,*scope,self.store.global_scope]
        if status!='all':where+=' AND status=?';args.append(status)
        if outcome:where+=' AND outcome=?';args.append(outcome)
        if q:where+=' AND (instr(title,?)>0 OR instr(goal,?)>0)';args +=[q,q]
        with self.store.connect() as db:
            total=db.execute('SELECT count(*) FROM episodes WHERE '+where,args).fetchone()[0]
            rows=db.execute('SELECT * FROM episodes WHERE '+where+' ORDER BY updated DESC LIMIT 30 OFFSET ?',[*args,(page-1)*30]).fetchall()
            jobs=[dict(r) for r in db.execute("SELECT id,task_id,thread_id,state,reason,attempts,manual FROM episode_runs WHERE space_id=? AND (scope_kind=? AND scope_id=? OR ?) AND (state IN ('collecting','pending','running','retry_wait','failed') OR (state='skipped' AND reason!='idle_superseded')) ORDER BY created DESC LIMIT 30",(sid,*scope,self.store.global_scope))]
        cfg=self.store.config();paused=self.registry.state()['active_profile_id']!=PROFILE or not((cfg.get("enabled",True) and cfg['learn_memories']) and cfg['langmem']['episodes'])
        return {'items':[self.public_row(r) for r in rows],'total':total,'page':page,'jobs':jobs,'paused':paused}

    def detail(self,eid,page=1):
        row=self.get(eid)
        with self.store.connect() as db:
            events=db.execute('''SELECT e.*,r.thread_id,r.run_id FROM episode_events e JOIN episode_runs r ON r.id=e.job_id WHERE r.task_id=? AND r.state NOT IN ('skipped','cancelled') ORDER BY e.created DESC,e.rowid DESC LIMIT 30 OFFSET ?''',(eid,(page-1)*30)).fetchall()
            total=db.execute("SELECT count(*) FROM episode_events e JOIN episode_runs r ON r.id=e.job_id WHERE r.task_id=? AND r.state NOT IN ('skipped','cancelled')",(eid,)).fetchone()[0]
            proof=db.execute('SELECT role,text,tool,status,created FROM episode_events WHERE id=?',(row['evidence_id'],)).fetchone()
            changes=[dict(r) for r in db.execute('SELECT action,version,reason,created FROM episode_changes WHERE episode_id=? ORDER BY id DESC LIMIT 30',(eid,))]
        return {**row,'outcome_evidence':dict(proof) if proof else None,'events':[{**dict(e),'artifact':json.loads(e['artifact']) if e['artifact'] else None} for e in events],'event_total':total,'page':page,'changes':changes}

    def check_version(self,db,eid,version):
        self.registry.require_active(PROFILE)
        row=db.execute('SELECT * FROM episodes WHERE id=? AND version=?',(eid,version)).fetchone()
        if not row or row['status'] in ('merged','deleted'):raise ValueError('经历版本已变化或已合并，请刷新后重试。')
        return row

    def edit(self,eid,body):
        with self.registry.guard(),self.store.connect() as db:
            row=self.check_version(db,eid,body.version)
            data=json.loads(row['body']);data.update(title=body.title,context=body.context,applicability=body.applicability)
            db.execute("UPDATE episodes SET title=?,body=?,outcome=?,basis='user_correction',evidence_id=NULL,locked=?,version=version+1,updated=? WHERE id=?",(body.title,encode(data),body.outcome,int(body.locked),time.time(),eid))
            db.execute('DELETE FROM episode_vectors WHERE episode_id=?',(eid,))
            db.execute('INSERT INTO episode_changes(episode_id,action,version,reason,created) VALUES (?,?,?,?,?)',(eid,'corrected',body.version+1,body.reason,time.time()))
            self.store.touch_epoch(db,row['space_id'])
        return self.get(eid)

    def state(self,eid,body):
        with self.registry.guard(),self.store.connect() as db:
            row=self.check_version(db,eid,body.version)
            db.execute('UPDATE episodes SET status=?,version=version+1,updated=? WHERE id=?',(body.status,time.time(),eid))
            db.execute('DELETE FROM episode_vectors WHERE episode_id=?',(eid,))
            self.store.touch_epoch(db,row['space_id'])
            db.execute('INSERT INTO episode_changes(episode_id,action,version,reason,created) VALUES (?,?,?,?,?)',(eid,body.status,body.version+1,'',time.time()))
        return self.get(eid)

    def merge(self,eid,body):
        if eid==body.target_id:raise ValueError('不能合并到自身。')
        with self.registry.guard(),self.store.connect() as db:
            source=self.check_version(db,eid,body.version);target=self.check_version(db,body.target_id,body.target_version)
            if source['space_id']!=target['space_id'] or scope_of(dict(source))!=scope_of(dict(target)) or source['status']!='active' or target['status']!='active':raise ValueError('只能合并同一空间、同一范围内的生效经历。')
            # Keep target narrative; preserve both event streams. Do not silently
            # select a success from incompatible outcomes during a manual merge.
            db.execute('UPDATE episode_runs SET task_id=? WHERE task_id=?',(body.target_id,eid))
            db.execute('UPDATE episode_tracks SET task_id=? WHERE task_id=?',(body.target_id,eid))
            db.execute("UPDATE episodes SET status='merged',merged_into=?,version=version+1,updated=? WHERE id=?",(body.target_id,time.time(),eid))
            db.execute("UPDATE episodes SET outcome='unknown',basis='merge_needs_review',evidence_id=NULL,locked=1,version=version+1,updated=? WHERE id=?",(time.time(),body.target_id))
            db.execute('DELETE FROM episode_vectors WHERE episode_id IN (?,?)',(eid,body.target_id))
            self.store.touch_epoch(db,source['space_id'])
            for key,version in [(eid,body.version),(body.target_id,body.target_version)]:
                db.execute('INSERT INTO episode_changes(episode_id,action,version,reason,created) VALUES (?,?,?,?,?)',(key,'merged',version+1,'已保留目标摘要及双方事件，需重新确认结果。',time.time()))
        return self.get(body.target_id)

    def job_action(self,sid,jid,action):
        self.memory.store.require_langmem(sid)
        with self.registry.guard(),self.store.connect() as db:
            self.registry.require_active(PROFILE)
            row=db.execute('SELECT * FROM episode_runs WHERE id=? AND space_id=?',(jid,sid)).fetchone()
            if not row or row['state'] in TERMINAL:raise ValueError('经历整理任务已处理，请刷新。')
            if action=='cancel':
                db.execute("UPDATE episode_runs SET state='cancelled',reason='user_cancelled',lease=NULL WHERE id=?",(jid,))
                db.execute('DELETE FROM episode_events WHERE job_id=?',(jid,))
            elif action in ('retry','flush'):
                if action=='retry' and row['state'] not in ('failed','retry_wait'):raise ValueError('此任务尚未失败。')
                if action=='flush' and row['state'] not in ('pending','retry_wait'):raise ValueError('此任务不在等待队列。')
                cfg=self.store.config()
                if not row['manual'] and not((cfg.get("enabled",True) and cfg['learn_memories']) and cfg['langmem']['episodes']):raise ValueError('请先开启经历自动整理。')
                if row['epoch']!=self.store.epoch(sid):raise ValueError('已有人工改动，不能重放旧经历来源。')
                frozen=json.loads(row['frozen']);frozen.update(cfg,activation_epoch=self.registry.state()['epoch'])
                db.execute("UPDATE episode_runs SET state='pending',reason='',attempts=0,available=0,frozen=? WHERE id=?",(encode(frozen),jid))
            else:raise ValueError('未知的任务操作。')
        return {'ok':True}

    def new_task(self,frozen,thread):
        with self.registry.guard(),self.store.connect() as db:
            if not self.allowed(frozen,True):raise ValueError('请先激活并启用 LangMem 方案。')
            sid=frozen['space_id'];scope=scope_of(frozen);self.store.validate_scope(sid,scope);key=uuid4().hex
            db.execute('INSERT INTO episode_tracks VALUES (?,?,?,?,?) ON CONFLICT(space_id,thread_id,scope_kind,scope_id) DO UPDATE SET task_id=excluded.task_id',(sid,thread,*scope,key))
        return {'task_id':key}

    def semantic_scores(self,frozen,query,rows):
        """Return semantic similarity while keeping keyword recall as a safe fallback."""
        if not rows or frozen.get('langmem',{}).get('retrieval')!='semantic':return {},False
        try:
            searcher=self.memory.searcher(frozen);embedding=searcher.embedding;profile=searcher.profile
            query_vector=embedding.get_query_embedding(query)
            if not query_vector or not all(isinstance(x,(int,float)) and math.isfinite(x) for x in query_vector):return {},False
            signature=hashlib.sha256(encode([profile.provider,profile.base_url,profile.model,len(query_vector),'episode-recall-v1']).encode()).hexdigest()
            qnorm=math.sqrt(sum(x*x for x in query_vector))
            if not qnorm:return {},False
            scores={}
            for row in rows:
                text=row['_search_text'];fingerprint=hashlib.sha256(text.encode()).hexdigest();vector=None
                with self.store.connect() as db:
                    cached=db.execute('SELECT version,fingerprint,vector FROM episode_vectors WHERE space_id=? AND episode_id=? AND signature=?',(frozen['space_id'],row['id'],signature)).fetchone()
                if cached and cached['version']==row['version'] and cached['fingerprint']==fingerprint:
                    vector=json.loads(cached['vector'])
                if vector is None:
                    vector=embedding.get_text_embedding(text)
                    if len(vector)!=len(query_vector) or not all(isinstance(x,(int,float)) and math.isfinite(x) for x in vector):continue
                    with self.registry.guard(),self.store.connect() as db:
                        live=db.execute("SELECT version FROM episodes WHERE id=? AND space_id=? AND status='active'",(row['id'],frozen['space_id'])).fetchone()
                        if self.registry.valid(frozen) and live and live['version']==row['version']:
                            db.execute('INSERT OR REPLACE INTO episode_vectors VALUES (?,?,?,?,?,?)',(frozen['space_id'],row['id'],row['version'],signature,fingerprint,encode(vector)))
                vnorm=math.sqrt(sum(x*x for x in vector))
                if vnorm:scores[row['id']]=sum(a*b for a,b in zip(query_vector,vector))/(qnorm*vnorm)
            return scores,True
        except Exception:
            # Episodic recall remains available when a local embedding service is offline.
            return {},False

    def select(self,frozen,query,limit=3):
        if frozen.get('engine')!='langmem' or not frozen.get('use_memories') or not self.registry.valid(frozen) or not self.store.config()['use_memories']:return []
        q=tokens(query)
        if not q:return []
        intent=recall_intent(query)
        scope=scope_of(frozen)
        with self.store.connect() as db:
            outcome_filter="" if intent=='history' else "AND outcome!='unknown'"
            rows=db.execute(f"SELECT * FROM episodes WHERE space_id=? AND status='active' {outcome_filter} AND ((scope_kind=? AND scope_id=?) OR (scope_kind='personal' AND scope_id='personal') OR ?) ORDER BY updated DESC",(frozen['space_id'],*scope,self.store.global_scope)).fetchall()
            from .lifecycle import unavailable
            rows=[dict(r) for r in rows if not unavailable(db,r['id'])]
        task_type=next((kind for kind,pattern in [('coding',r'代码|调试|编程|python|javascript|bug'),('research',r'研究|论文|资料|搜索'),('writing',r'写作|文章|汇报|邮件'),('learning',r'学习|概念|理解|练习')] if re.search(pattern,query,re.I)),None)
        prepared=[]
        for row in rows:
            body=json.loads(row['body']);text=search_text(row,body);terms=tokens(text);overlap=len(q&terms)
            applicability_overlap=len(q & tokens(body.get('applicability','')))
            prepared.append({**row,'_body':body,'_search_text':text,'_overlap':overlap,'_keyword_score':overlap/max(1,min(len(q),len(terms))), '_applicability_overlap':applicability_overlap})
        semantic,semantic_available=self.semantic_scores(frozen,query,prepared)
        keyword_rank={row['id']:rank for rank,row in enumerate(sorted((r for r in prepared if r['_overlap']),key=lambda r:(-r['_keyword_score'],-r['updated'])),1)}
        semantic_rank={mid:rank for rank,(mid,_) in enumerate(sorted(((mid,score) for mid,score in semantic.items() if score>=frozen['langmem']['similarity_threshold']),key=lambda item:-item[1]),1)}
        hits=[]
        for row in prepared:
            semantic_score=semantic.get(row['id']);kw_rank=keyword_rank.get(row['id']);sem_rank=semantic_rank.get(row['id'])
            if not kw_rank and not sem_rank:continue
            body=row['_body']
            age=max(0,(time.time()-row['updated'])/86400)
            # Reciprocal-rank fusion combines unlike score scales. The remaining
            # terms break close ties without treating recency as proof of quality.
            score=(1/(60+kw_rank) if kw_rank else 0)+(1/(60+sem_rank) if sem_rank else 0)
            score+=(0.012 if task_type and row['task_type']==task_type else 0)+min(.012,row['_applicability_overlap']*.004)
            score+=(.008 if scope_of(row)==scope else 0)+(.006 if row['basis']=='user_feedback' else 0)+.002/(1+age/30)
            reason='semantic_and_keyword_case' if kw_rank and sem_rank else 'semantic_case' if sem_rank else 'keyword_case'
            hits.append({'id':row['id'],'version':row['version'],'title':row['title'],'task_type':row['task_type'],'outcome':row['outcome'],'basis':row['basis'],'context':body.get('context','')[:450],'applicability':body.get('applicability',''),'lessons':[n['text'] for n in body.get('lessons',[])],'scope_kind':row['scope_kind'],'scope_id':row['scope_id'],'score':round(score,4),'semantic_score':round(semantic_score,3) if semantic_score is not None else None,'keyword_matches':row['_overlap'],'retrieval':'hybrid' if semantic_available else 'keyword_fallback','recall_intent':intent,'memory_type':'episode','reason':reason,'included':False})
        return sorted(hits,key=lambda r:r['score'],reverse=True)[:limit]

    def artifact(self,eid,event_id):
        self.get(eid)
        with self.store.connect() as db:
            row=db.execute('SELECT e.artifact FROM episode_events e JOIN episode_runs r ON r.id=e.job_id WHERE r.task_id=? AND e.id=?',(eid,event_id)).fetchone()
        if not row or not row['artifact']:raise ValueError('此来源没有关联产物。')
        ref=json.loads(row['artifact'])
        from personal_workbench.workspace import ARTIFACT_SUFFIXES, MAX_ARTIFACT_BYTES, read_bytes
        try:data=read_bytes(self.memory.settings.outputs_dir/ref['thread_id'],ref['name'],ARTIFACT_SUFFIXES,MAX_ARTIFACT_BYTES)
        except (ValueError,OSError):raise ValueError('产物已移动或不可用，原保存凭据仍保留。') from None
        return {'title':ref['name'],'content':data.decode('utf-8'),'matches_saved':hashlib.sha256(data).hexdigest()==ref['sha256']}
