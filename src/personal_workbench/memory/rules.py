"""LangMem-owned procedural lifecycle: immutable versions, holdout gates, explicit activation."""
import hashlib
import json
import math
import re
import time
from dataclasses import asdict
from uuid import uuid4
from .foundation import PERSONAL, scope_of
from .learning import EXCLUDED
from .rule_schema import RuleProposal, RuleJudge
from .rule_evaluation import POLICY, CASES, SUITE_VERSION, checks

PROFILE='langmem-default'
ALWAYS_LIMIT=5
CONDITIONAL_LIMIT=3
COMMON_TERMS={
    '用户','本轮','任务','回答','协作','方式','时候','需要','进行','使用','一个','这个','帮我','请问','希望','如果','当前',
    'the','and','for','with','when','user','answer','response','please','this','that','from','into','your',
}
LEGACY_ALWAYS=re.compile(r'所有(?:对话|回答|任务)|每次(?:对话|回答)|任何任务|始终|\balways\b|\bevery\s+(?:answer|response|conversation|turn|task)\b',re.I)
def encode(v):return json.dumps(v,ensure_ascii=False,sort_keys=True,default=str)
def digest(v):return hashlib.sha256(encode(v).encode()).hexdigest()

def normalize_rule_data(data):
    data=dict(data)
    if 'always_on' not in data:data['always_on']=bool(LEGACY_ALWAYS.search(data.get('applies_when','')))
    data['priority']=max(1,min(5,int(data.get('priority',3))))
    return data

def rule_tokens(text):
    lowered=text.lower()
    words=set(re.findall(r'[a-z0-9_+#.-]{2,}|[\u4e00-\u9fff]{2,}',lowered))
    words|={lowered[i:i+2] for i in range(len(lowered)-1) if all('\u4e00'<=c<='\u9fff' for c in lowered[i:i+2])}
    return {term for term in words if term not in COMMON_TERMS}

def rule_relevance(data,query):
    """Deterministic first-pass routing; the selected fragment still carries its conditions."""
    query_terms=rule_tokens(query)
    if not query_terms:return 0.0
    condition=rule_tokens(data.get('applies_when',''))
    title=rule_tokens(data.get('title',''))
    instruction=rule_tokens(data.get('instruction',''))
    overlap_condition=query_terms & condition
    overlap_other=query_terms & (title|instruction)
    if not overlap_condition and not overlap_other:return 0.0
    # Applicability terms are stronger than words that only occur in the behavior text.
    score=len(overlap_condition)*2.0+len(overlap_other)*0.7
    score+=len(overlap_condition)/max(1,len(condition))
    return round(score+data.get('priority',3)*0.01,3)

class RuleInterrupted(Exception):
    pass

class InvalidFragment(Exception):
    pass

class Rules:
    def __init__(self,memory):
        self.memory=memory;self.store=memory.store.stores['langmem'];self.registry=memory.store.registry
        with self.store.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS collaboration_rules(id TEXT PRIMARY KEY,space_id TEXT NOT NULL,scope_kind TEXT NOT NULL,scope_id TEXT NOT NULL,topic TEXT NOT NULL,revision INTEGER NOT NULL,active_version INTEGER,state TEXT NOT NULL DEFAULT 'candidate',created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(space_id,scope_kind,scope_id,topic));
            CREATE TABLE IF NOT EXISTS rule_versions(rule_id TEXT,version INTEGER,data TEXT NOT NULL,sources TEXT NOT NULL,prompt TEXT NOT NULL DEFAULT '',created REAL NOT NULL,PRIMARY KEY(rule_id,version));
            CREATE TABLE IF NOT EXISTS rule_jobs(id TEXT PRIMARY KEY,rule_id TEXT NOT NULL,version INTEGER NOT NULL,kind TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',frozen TEXT NOT NULL,revision INTEGER NOT NULL,environment TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,available REAL NOT NULL,lease TEXT,lease_until REAL,result TEXT,created REAL NOT NULL,updated REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS rule_jobs_due ON rule_jobs(state,available);
            CREATE TABLE IF NOT EXISTS rule_suppressions(rule_id TEXT,source_hash TEXT,PRIMARY KEY(rule_id,source_hash));
            CREATE TABLE IF NOT EXISTS rule_changes(id INTEGER PRIMARY KEY AUTOINCREMENT,rule_id TEXT,action TEXT,version INTEGER,revision INTEGER,created REAL);
            CREATE TABLE IF NOT EXISTS rule_vectors(space_id TEXT,rule_id TEXT,version INTEGER,signature TEXT,fingerprint TEXT,vector TEXT,PRIMARY KEY(space_id,rule_id,version,signature));
            ''')
            if 'rejected' not in {r[1] for r in db.execute('PRAGMA table_info(rule_versions)')}:
                db.execute('ALTER TABLE rule_versions ADD COLUMN rejected INTEGER NOT NULL DEFAULT 0')

    def row(self,db,rid):
        row=db.execute('SELECT * FROM collaboration_rules WHERE id=?',(rid,)).fetchone()
        if not row or row['state']=='deleted':raise ValueError('协作规则不存在。')
        return dict(row)

    def active(self,db,sid,scope,exclude=''):
        rows=db.execute('''SELECT r.*,v.data,v.prompt FROM collaboration_rules r JOIN rule_versions v ON r.id=v.rule_id AND r.active_version=v.version WHERE r.space_id=? AND r.state='active' AND r.id!=? AND ((r.scope_kind=? AND r.scope_id=?) OR (r.scope_kind='personal' AND r.scope_id='personal')) ORDER BY r.scope_kind,r.topic''',(sid,exclude,*scope)).fetchall()
        if self.store.global_scope:
            rows=db.execute("SELECT r.*,v.data,v.prompt FROM collaboration_rules r JOIN rule_versions v ON r.id=v.rule_id AND r.active_version=v.version WHERE r.space_id=? AND r.state='active' AND r.id!=? ORDER BY r.topic",(sid,exclude)).fetchall()
        from .lifecycle import unavailable
        rows=[r for r in rows if not unavailable(db,r['id'])]
        if self.store.global_scope:return [dict(r) for r in rows]
        # More specific scope overrides the same topic in the general scope.
        selected={r['topic']:dict(r) for r in rows if r['scope_kind']=='personal'}
        selected.update({r['topic']:dict(r) for r in rows if r['scope_kind']!='personal'})
        return sorted(selected.values(),key=lambda r:r['topic'])

    def environment(self,db,row):
        from personal_workbench.app_settings import AppSettings
        cfg=self.store.config();runtime=AppSettings(self.memory.settings).runtime(cfg.get('model_profile_id'))
        # Hash runtime values without exposing configuration or credentials.
        return digest({'suite':SUITE_VERSION,'runtime':asdict(runtime),'revision':cfg['revision'],
                       'others':[(r['id'],r['active_version'],digest(r['prompt'])) for r in self.active(db,row['space_id'],scope_of(row),row['id'])]})

    def sources(self,body,sid):
        sources=[]
        if body.feedback.strip():
            if EXCLUDED.search(body.feedback):raise ValueError('反馈包含不记录要求或敏感标记，未保存规则。')
            sources.append({'kind':'feedback','text':body.feedback.strip(),'hash':digest(body.feedback.strip())})
        for eid in dict.fromkeys(body.episode_ids):
            case=self.memory.episodes.get(eid)
            if case['space_id']!=sid or scope_of(case) not in (scope_of(body.model_dump()),PERSONAL):raise ValueError('来源经历不属于当前记忆空间或范围。')
            if case['status']!='active' or case['outcome'] not in ('success','partial') or case['basis'] not in ('user_feedback','user_correction'):
                raise ValueError('请选择有用户结果确认的成功或部分成功经历；仅保存产物不足以推导协作规则。')
            with self.store.connect() as db:
                threads=[r[0] for r in db.execute('SELECT DISTINCT thread_id FROM episode_runs WHERE task_id=?',(eid,))]
            detail=self.memory.episodes.detail(eid)
            proof=(detail.get('outcome_evidence') or {}).get('text') or next((c['reason'] for c in detail['changes'] if c['action']=='corrected'),'')
            sources.append({'evidence':proof,'kind':'episode','id':eid,'version':case['version'],'threads':threads,'title':case['title'],'text':case['body']['context'],'outcome':case['outcome'],'basis':case['basis'],'hash':digest('episode:'+eid)})
        cases=[s for s in sources if s['kind']=='episode']
        if not body.feedback.strip() and (len(cases)<2 or len({t for s in cases for t in s['threads']})<2):
            raise ValueError('请提供一次明确反馈，或选择至少两个不同对话中有结果确认的任务经历。')
        return sources

    def check_sources(self,db,row,sources,*,restoring=False):
        from .lifecycle import unavailable
        if unavailable(db,row['id']):raise ValueError('记忆对象已删除或过期。')
        if row['state']=='blocked' and not restoring:raise ValueError('此主题已停用；请明确评估并恢复历史版本，不会从旧来源重新生成。')
        for source in sources:
            blocked=db.execute('''SELECT s.rule_id FROM rule_suppressions s JOIN collaboration_rules r ON r.id=s.rule_id WHERE r.space_id=? AND s.source_hash=?''',(row['space_id'],source['hash'])).fetchall()
            if any(r[0]!=row['id'] or not restoring for r in blocked):raise ValueError('这些旧来源已被停用规则抑制，不能重新生成规则。')
            if source['kind']=='episode':
                if unavailable(db,source['id']):raise ValueError('来源经历已删除或过期。')
                live=db.execute('SELECT version,status,outcome,basis FROM episodes WHERE id=?',(source['id'],)).fetchone()
                if not live or live['version']!=source['version'] or live['status']!='active' or live['outcome'] not in ('success','partial') or live['basis'] not in ('user_feedback','user_correction'):
                    raise ValueError('来源经历已变化，请重新选择并提出候选。')

    def log(self,db,row,action,version):
        db.execute('INSERT INTO rule_changes(rule_id,action,version,revision,created) VALUES (?,?,?,?,?)',(row['id'],action,version,row['revision'],time.time()))

    def enqueue(self,db,row,version,kind):
        jid=uuid4().hex;frozen=self.memory.freeze({'space_id':row['space_id'],'scope_kind':row['scope_kind'],'scope_id':row['scope_id']})
        now=time.time()
        db.execute('''INSERT INTO rule_jobs(id,rule_id,version,kind,state,frozen,revision,environment,available,created,updated) VALUES (?,?,?,?,'pending',?,?,?,?,?,?)''',(jid,row['id'],version,kind,encode(frozen),row['revision'],self.environment(db,row),now,now,now))
        for other in self.active(db,row['space_id'],scope_of(row),row['id']):
            db.execute('INSERT OR IGNORE INTO lifecycle_edges VALUES (?,?,?,?)',(other['id'],row['id'],'rule_evaluation_input',time.time()))
        return jid

    def propose(self,sid,body):
        self.memory.store.require_langmem(sid)
        with self.registry.guard(),self.store.connect() as db:
            self.registry.require_active(PROFILE);self.store.validate_scope(sid,scope_of(body.model_dump()))
            if EXCLUDED.search(encode(body.model_dump(exclude={'episode_ids'}))):raise ValueError('规则包含不记录要求或敏感标记。')
            existing=db.execute('SELECT * FROM collaboration_rules WHERE space_id=? AND scope_kind=? AND scope_id=? AND topic=?',(sid,body.scope_kind,body.scope_id,body.topic)).fetchone()
            if existing:
                row=dict(existing)
                if row['revision']!=body.revision:raise ValueError('此主题已有规则或版本已变化，请打开该规则修改。')
            else:
                if body.revision:raise ValueError('规则版本已变化，请刷新。')
                row={'id':uuid4().hex,'space_id':sid,'scope_kind':body.scope_kind,'scope_id':body.scope_id,'topic':body.topic,'revision':0,'state':'candidate'}
            sources=self.sources(body,sid);self.check_sources(db,row,sources)
            version=db.execute('SELECT coalesce(max(version),0)+1 FROM rule_versions WHERE rule_id=?',(row['id'],)).fetchone()[0]
            row['revision']+=1;now=time.time()
            db.execute('''INSERT INTO collaboration_rules(id,space_id,scope_kind,scope_id,topic,revision,created,updated) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,updated=excluded.updated''',(row['id'],sid,body.scope_kind,body.scope_id,body.topic,row['revision'],now,now))
            db.execute('INSERT INTO rule_versions(rule_id,version,data,sources,created) VALUES (?,?,?,?,?)',(row['id'],version,encode(body.model_dump(exclude={'revision','feedback','episode_ids'})),encode(sources),now))
            self.log(db,row,'proposed',version);jid=self.enqueue(db,row,version,'generate')
        return {'id':row['id'],'version':version,'job_id':jid}

    def version(self,db,rid,version):
        row=db.execute('SELECT * FROM rule_versions WHERE rule_id=? AND version=?',(rid,version)).fetchone()
        if not row:raise ValueError('规则版本不存在。')
        return {**dict(row),'data':normalize_rule_data(json.loads(row['data'])),'sources':json.loads(row['sources'])}

    def checked(self,db,rid,action):
        self.registry.require_active(PROFILE);row=self.row(db,rid)
        if row['revision']!=action.revision:raise ValueError('规则状态已变化，请刷新后重试。')
        return row,self.version(db,rid,action.version)

    def evaluate(self,rid,action):
        with self.registry.guard(),self.store.connect() as db:
            row,version=self.checked(db,rid,action)
            if version['rejected']:raise ValueError('此候选已拒绝，请创建新的候选版本。')
            if not version['prompt']:raise ValueError('请先等待候选行为片段生成。')
            self.check_sources(db,row,version['sources'],restoring=True)
            prior=db.execute("SELECT id FROM rule_jobs WHERE rule_id=? AND version=? AND kind='evaluate' AND state IN ('pending','running','retry_wait')",(rid,action.version)).fetchone()
            return {'job_id':prior[0] if prior else self.enqueue(db,row,action.version,'evaluate')}

    def activate(self,rid,action):
        with self.registry.guard(),self.store.connect() as db:
            row,version=self.checked(db,rid,action)
            if version['rejected']:raise ValueError('此候选已拒绝，请创建新的候选版本。')
            self.check_sources(db,row,version['sources'],restoring=True)
            job=db.execute("SELECT * FROM rule_jobs WHERE rule_id=? AND version=? AND kind='evaluate' AND state='completed' ORDER BY created DESC LIMIT 1",(rid,action.version)).fetchone()
            if not job or not json.loads(job['result'])['passed'] or job['environment']!=self.environment(db,row):raise ValueError('此版本未通过当前配置的评估，请先重新评估。')
            action_name='rollback' if row['active_version'] and action.version<row['active_version'] else 'activated'
            row['revision']+=1
            db.execute("UPDATE collaboration_rules SET active_version=?,state='active',revision=?,updated=? WHERE id=?",(action.version,row['revision'],time.time(),rid))
            db.execute('DELETE FROM rule_suppressions WHERE rule_id=?',(rid,));self.log(db,row,action_name,action.version)
        return self.detail(rid)

    def reject(self,rid,action):
        with self.registry.guard(),self.store.connect() as db:
            row,version=self.checked(db,rid,action)
            if row['active_version']==action.version:raise ValueError('生效版本请使用停用操作。')
            if version['rejected']:raise ValueError('此候选已拒绝。')
            row['revision']+=1
            db.execute('UPDATE rule_versions SET rejected=1 WHERE rule_id=? AND version=?',(rid,action.version))
            db.execute('UPDATE collaboration_rules SET revision=?,updated=? WHERE id=?',(row['revision'],time.time(),rid))
            db.execute("UPDATE rule_jobs SET state='cancelled',reason='candidate_rejected',lease=NULL WHERE rule_id=? AND version=? AND state IN ('pending','running','retry_wait')",(rid,action.version))
            self.log(db,row,'rejected',action.version)
        return self.detail(rid)

    def disable(self,rid,action):
        with self.registry.guard(),self.store.connect() as db:
            row,version=self.checked(db,rid,action);row['revision']+=1
            db.execute("UPDATE collaboration_rules SET active_version=NULL,state='blocked',revision=?,updated=? WHERE id=?",(row['revision'],time.time(),rid))
            for record in db.execute('SELECT sources FROM rule_versions WHERE rule_id=?',(rid,)):
                for source in json.loads(record[0]):db.execute('INSERT OR IGNORE INTO rule_suppressions VALUES (?,?)',(rid,source['hash']))
            db.execute("UPDATE rule_jobs SET state='cancelled',reason='rule_disabled',lease=NULL WHERE rule_id=? AND state IN ('pending','running','retry_wait')",(rid,))
            self.log(db,row,'disabled',action.version)
        return self.detail(rid)

    def detail(self,rid):
        with self.store.connect() as db:
            row=self.row(db,rid)
            versions=[self.version(db,rid,r[0]) for r in db.execute('SELECT version FROM rule_versions WHERE rule_id=? ORDER BY version DESC',(rid,))]
            for version in versions:
                try:self.check_sources(db,row,version['sources'],restoring=True);version['sources_current']=True
                except ValueError:version['sources_current']=False
            jobs=[{**dict(r),'result':json.loads(r['result']) if r['result'] else None} for r in db.execute('SELECT id,version,kind,state,reason,attempts,result,created FROM rule_jobs WHERE rule_id=? ORDER BY created DESC LIMIT 30',(rid,))]
            environment=self.environment(db,row)
            for job in jobs:
                if job['kind']=='evaluate' and job['result']:
                    stored=db.execute('SELECT environment FROM rule_jobs WHERE id=?',(job['id'],)).fetchone()[0]
                    job['result']['current_environment']=stored==environment
            history=[dict(r) for r in db.execute('SELECT action,version,revision,created FROM rule_changes WHERE rule_id=? ORDER BY id DESC LIMIT 50',(rid,))]
        return {**row,'versions':versions,'jobs':jobs,'history':history,'suite_version':SUITE_VERSION}

    def listing(self,sid,scope=PERSONAL):
        self.memory.store.require_langmem(sid);self.store.validate_scope(sid,scope)
        with self.store.connect() as db:
            result=[]
            for record in db.execute("SELECT * FROM collaboration_rules WHERE space_id=? AND (scope_kind=? AND scope_id=? OR ?) AND state!='deleted' ORDER BY topic",(sid,*scope,self.store.global_scope)):
                row=dict(record)
                latest=db.execute('SELECT max(version) FROM rule_versions WHERE rule_id=?',(row['id'],)).fetchone()[0]
                versions=[self.version(db,row['id'],latest)]
                if row['active_version'] and row['active_version']!=latest:versions.append(self.version(db,row['id'],row['active_version']))
                result.append({**row,'versions':versions,'jobs':[],'history':[]})
        return {'items':result}

    def job_action(self,jid,action):
        with self.registry.guard(),self.store.connect() as db:
            self.registry.require_active(PROFILE);job=db.execute('SELECT * FROM rule_jobs WHERE id=?',(jid,)).fetchone()
            if not job:raise ValueError('规则整理任务不存在。')
            row=self.row(db,job['rule_id'])
            if action=='cancel' and job['state'] in ('pending','running','retry_wait','failed'):
                db.execute("UPDATE rule_jobs SET state='cancelled',lease=NULL,reason='user_cancelled' WHERE id=?",(jid,))
            elif action=='retry' and job['state'] in ('failed','retry_wait'):
                if row['revision']!=job['revision'] or self.environment(db,row)!=job['environment']:raise ValueError('规则或模型配置已变化，请重新提出或评估。')
                db.execute("UPDATE rule_jobs SET state='pending',attempts=0,available=0,reason='' WHERE id=?",(jid,))
            else:raise ValueError('此任务不能执行该操作，请刷新。')
        return {'ok':True}

    def run_once(self,stop=None,force=False):
        with self.registry.guard(),self.store.connect() as db:
            if self.registry.state()['active_profile_id']!=PROFILE:return False
            now=time.time();db.execute("UPDATE rule_jobs SET state='pending',lease=NULL WHERE state='running' AND lease_until<?",(now,))
            raw=db.execute("SELECT * FROM rule_jobs WHERE state IN ('pending','retry_wait') AND (available<=? OR ?) ORDER BY created LIMIT 1",(now,int(force))).fetchone()
            if not raw:return False
            job=dict(raw);row=self.row(db,job['rule_id']);version=self.version(db,job['rule_id'],job['version'])
            if row['revision']!=job['revision'] or self.environment(db,row)!=job['environment']:
                db.execute("UPDATE rule_jobs SET state='skipped',reason='state_changed' WHERE id=?",(job['id'],));return True
            try:self.check_sources(db,row,version['sources'],restoring=job['kind']=='evaluate')
            except ValueError:
                db.execute("UPDATE rule_jobs SET state='skipped',reason='sources_changed' WHERE id=?",(job['id'],));return True
            lease=uuid4().hex;frozen=json.loads(job['frozen']);frozen['activation_epoch']=self.registry.state()['epoch']
            db.execute("UPDATE rule_jobs SET state='running',lease=?,lease_until=?,attempts=attempts+1,updated=? WHERE id=?",(lease,now+1200,now,job['id']))
            others=self.active(db,row['space_id'],scope_of(row),row['id'])
            current_version=self.version(db,row['id'],row['active_version']) if row['active_version'] else None
            current=self.fragment(current_version['data'],current_version['prompt']) if current_version else ''
        def keep_going():
            with self.registry.guard(),self.store.connect() as db:
                current_job=db.execute('SELECT state,lease FROM rule_jobs WHERE id=?',(job['id'],)).fetchone()
                live_rule=self.row(db,row['id'])
                if (stop and stop.is_set()) or not self.registry.valid(frozen) or current_job['state']!='running' or current_job['lease']!=lease or live_rule['revision']!=job['revision'] or self.environment(db,live_rule)!=job['environment']:
                    raise RuleInterrupted()
        try:
            keep_going()
            engine=self.memory.engine(frozen)
            if job['kind']=='generate':
                prompt=engine.optimize_rule(version['data'],version['sources'],current)
                if not isinstance(prompt,str) or not all(checks(version['data'],prompt).values()):raise InvalidFragment()
                report={'generated':True}
            else:
                prompt=version['prompt'];report=self.test(engine,version,others,current,keep_going)
            with self.registry.guard(),self.store.connect() as db:
                live=db.execute('SELECT state,lease FROM rule_jobs WHERE id=?',(job['id'],)).fetchone()
                if live['state']!='running' or live['lease']!=lease:return True
                if not self.registry.valid(frozen) or (stop and stop.is_set()):
                    db.execute("UPDATE rule_jobs SET state='pending',lease=NULL,reason='profile_paused' WHERE id=?",(job['id'],));return True
                row=self.row(db,row['id'])
                if row['revision']!=job['revision'] or self.environment(db,row)!=job['environment']:
                    db.execute("UPDATE rule_jobs SET state='skipped',lease=NULL,reason='state_changed' WHERE id=?",(job['id'],));return True
                self.check_sources(db,row,version['sources'],restoring=job['kind']=='evaluate')
                if job['kind']=='generate':db.execute('UPDATE rule_versions SET prompt=? WHERE rule_id=? AND version=? AND prompt=\'\'',(prompt,row['id'],job['version']))
                db.execute("UPDATE rule_jobs SET state='completed',reason='',result=?,lease=NULL,updated=? WHERE id=?",(encode(report),time.time(),job['id']))
                self.log(db,row,'generated' if job['kind']=='generate' else 'evaluated',job['version'])
        except InvalidFragment:
            with self.registry.guard(),self.store.connect() as db:
                db.execute("UPDATE rule_jobs SET state='failed',reason='fragment_rejected',lease=NULL WHERE id=? AND state='running' AND lease=?",(job['id'],lease))
        except RuleInterrupted:
            with self.registry.guard(),self.store.connect() as db:
                changed=self.row(db,row['id'])['revision']!=job['revision'] or self.environment(db,row)!=job['environment']
                db.execute("UPDATE rule_jobs SET state=?,reason=?,lease=NULL,attempts=max(0,attempts-1) WHERE id=? AND state='running' AND lease=?",('skipped' if changed else 'pending','state_changed' if changed else 'profile_paused',job['id'],lease))
        except Exception:
            with self.registry.guard(),self.store.connect() as db:
                db.execute("UPDATE rule_jobs SET state=CASE WHEN attempts>=2 THEN 'failed' ELSE 'retry_wait' END,reason='model_failed',available=?,lease=NULL WHERE id=? AND state='running' AND lease=?",(time.time()+30,job['id'],lease))
        return True

    def test(self,engine,version,others,current,keep_going=lambda:None):
        candidate=self.fragment(version['data'],version['prompt']);common='\n'.join(self.fragment(json.loads(r['data']),r['prompt']) for r in others if r['topic']!=version['data']['topic'])
        fallback='\n'.join(self.fragment(json.loads(r['data']),r['prompt']) for r in others if r['topic']==version['data']['topic'])
        baseline=POLICY+common+'\n'+(current or fallback);proposed=POLICY+common+'\n'+candidate
        outputs=[];complete=True
        for case in CASES:
            keep_going();before=engine.rule_answer(baseline,case['query'])
            keep_going();after=engine.rule_answer(proposed,case['query'])
            complete=complete and len(before)<=4000 and len(after)<=4000
            outputs.append({**case,'before':before[:4000],'after':after[:4000],'truncated':len(before)>4000 or len(after)>4000})
        deterministic={**checks(version['data'],version['prompt']),'responses_complete':complete,'current_request_first':next(c for c in outputs if c['id']=='override')['after'].strip()=='P4_OK'}
        keep_going()
        judge=RuleJudge.model_validate(engine.judge_rule(version['data'],version['sources'],version['prompt'],outputs))
        passed=all(deterministic.values()) and all(getattr(judge,k) for k in RuleJudge.model_fields if k!='explanation')
        return {'passed':passed,'suite':SUITE_VERSION,'checks':deterministic,'judge':judge.model_dump(),'cases':outputs,'review_method':'separate_model_call','created':time.time()}

    @staticmethod
    def fragment(data,prompt):
        return encode({'协作方式':prompt,'仅当':data['applies_when'],'排除':data['exclusions']})

    def semantic_scores(self,frozen,query,rows):
        """Optional semantic supplement using the configured LangMem embedding model."""
        if not rows or frozen.get('langmem',{}).get('retrieval')!='semantic':return {}
        try:
            searcher=self.memory.searcher(frozen);embedding=searcher.embedding
            query_vector=embedding.get_query_embedding(query)
            profile=searcher.profile
            signature=digest([profile.provider,profile.base_url,profile.model,len(query_vector),'rule-recall-v1'])
            if not query_vector or not all(math.isfinite(x) for x in query_vector):return {}
            scores={}
            for row in rows:
                text=row.pop('_search_text');fingerprint=digest(text);vector=None
                with self.store.connect() as db:
                    cached=db.execute('SELECT fingerprint,vector FROM rule_vectors WHERE space_id=? AND rule_id=? AND version=? AND signature=?',(frozen['space_id'],row['id'],row['version'],signature)).fetchone()
                if cached and cached['fingerprint']==fingerprint:
                    vector=json.loads(cached['vector'])
                if not vector:
                    vector=embedding.get_text_embedding(text)
                    if len(vector)!=len(query_vector) or not all(math.isfinite(x) for x in vector):continue
                    with self.registry.guard(),self.store.connect() as db:
                        live=db.execute("SELECT active_version FROM collaboration_rules WHERE id=? AND space_id=? AND state='active'",(row['id'],frozen['space_id'])).fetchone()
                        if self.registry.valid(frozen) and live and live['active_version']==row['version']:
                            db.execute('INSERT OR REPLACE INTO rule_vectors VALUES (?,?,?,?,?,?)',(frozen['space_id'],row['id'],row['version'],signature,fingerprint,encode(vector)))
                qnorm=math.sqrt(sum(x*x for x in query_vector));vnorm=math.sqrt(sum(x*x for x in vector))
                if qnorm and vnorm:scores[row['id']]=sum(a*b for a,b in zip(query_vector,vector))/(qnorm*vnorm)
            return scores
        except (ValueError,TypeError,KeyError,ImportError):
            # Rule routing may fall back to exact terms; factual semantic recall keeps its own error reporting.
            return {}

    def select(self,frozen,query=None):
        if frozen.get('engine')!='langmem' or not frozen.get('enabled',True) or not frozen.get('use_memories') or not self.store.config()['use_memories'] or not self.registry.valid(frozen):return []
        with self.registry.guard(),self.store.connect() as db:
            results=[]
            for r in self.active(db,frozen['space_id'],scope_of(frozen)):
                version=self.version(db,r['id'],r['active_version'])
                try:self.check_sources(db,r,version['sources'])
                except ValueError:continue
                data=version['data'];always=data['always_on'];priority=data['priority']
                score=100.0+priority if always else rule_relevance(data,query or '')
                results.append({'id':r['id'],'version':r['active_version'],'memory_type':'rule','scope_kind':r['scope_kind'],'scope_id':r['scope_id'],'prompt':self.fragment(data,r['prompt']),'included':False,'reason':'always_on_rule' if always else 'related_rule','score':score,'always_on':always,'priority':priority,'updated':r['updated'],'_search_text':' '.join([data.get('title',''),data.get('applies_when',''),data.get('instruction','')])})
            # Calls without a query are inventory/control-plane reads. Runtime recall always supplies one.
            if query is None:
                for row in results:row.pop('_search_text',None)
                return sorted(results,key=lambda row:(-row['priority'],-row['updated'],row['id']))
            semantic=self.semantic_scores(frozen,query,[r for r in results if not r['always_on']])
            threshold=frozen.get('langmem',{}).get('similarity_threshold',.3)
            for row in results:
                value=semantic.get(row['id'])
                if value is not None:
                    row['semantic_score']=round(value,3)
                    if value>=threshold:row['score']=round(row['score']+value*1.5+row['priority']*.01,3)
                row.pop('_search_text',None)
            always=sorted((r for r in results if r['always_on']),key=lambda row:(-row['priority'],-row['updated'],row['id']))[:ALWAYS_LIMIT]
            related=sorted((r for r in results if not r['always_on'] and r['score']>0),key=lambda row:(-row['score'],-row['priority'],-row['updated'],row['id']))[:CONDITIONAL_LIMIT]
            return always+related
