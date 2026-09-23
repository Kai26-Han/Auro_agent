"""P6 Mem0 native lifecycle, persistent operation journal and serial local SDK."""
import fcntl
import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from importlib.metadata import version
from uuid import uuid4
from .mem0_native_store import NativeStore,PROFILE,TERMINAL
from .mem0_native_bridge import WriteBoundary,NativeLLM,EXTRACTION,UPDATE,EXCLUDED,fingerprint
from .mem0_engine import Mem0Engine,_lock


class NativeMem0:
    def __init__(self,memory,sdk_factory=None,channel="ordinary"):
        self.memory=memory;self.registry=memory.store.registry
        self.legacy=memory.store.stores['mem0'];self.store=NativeStore(self.registry,channel);self.channel_kind=channel
        self.sdk_factory=sdk_factory;self.fault=lambda stage,aid:None

    def channel(self,kind):
        return NativeMem0(self.memory,self.sdk_factory,channel=kind)

    @property
    def ready(self):return self.store.ready if self.channel_kind=='ordinary' else self.channel('ordinary').store.ready

    def bootstrap_empty(self):
        with self.registry.guard(),self.legacy.connect() as db:
            # Never silently abandon older content, histories or deletion barriers.
            if self.ready:return
            if not db.execute('SELECT 1 FROM memories LIMIT 1').fetchone() and not db.execute('SELECT 1 FROM blocked_hashes LIMIT 1').fetchone():
                self.store.set_meta('published',True)

    def require_space(self,sid):
        if self.channel_kind not in ('ordinary','event','procedure'):raise ValueError('此 Mem0 记忆通道尚未开放。')
        if self.legacy.space(sid)['engine']!='mem0':raise ValueError('此功能只用于 Mem0 空间。')

    def frozen(self,sid):
        self.require_space(sid);self.registry.require_active(PROFILE)
        return {**self.legacy.config(),'space_id':sid,'memory_profile_id':PROFILE,'activation_epoch':self.registry.state()['epoch']}

    def check_frozen(self,frozen,write=False):
        self.require_space(frozen["space_id"])
        if not self.registry.valid(frozen):raise ValueError('Mem0 方案已切换，旧操作不能继续写入。')
        cfg=self.legacy.config()
        if cfg['revision']!=frozen['revision']:raise ValueError('Mem0 配置已改变，请重新发起操作。')
        if write and frozen.get('_infer') and not (cfg.get("enabled",True) and cfg['learn_memories']):raise ValueError('Mem0 自动学习已关闭。')
        if write and self.channel_kind=='procedure' and frozen.get('_infer') and not cfg['mem0'].get('procedures',True):raise ValueError('方法整理已关闭。')
        if write and self.channel_kind=='event' and frozen.get('_infer') and not cfg['mem0'].get('events',True):raise ValueError('事件整理已关闭。')

    @contextmanager
    def serial(self):
        # All Qdrant opens use this lock, including reads and reconciliation.
        with _lock,(self.store.base_root/'operation.lock').open('a') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX)
            try:yield
            finally:fcntl.flock(handle,fcntl.LOCK_UN)

    @contextmanager
    def client(self,admin=False):
        if version('mem0ai')!='1.0.11' or version('qdrant-client')!='1.19.1':raise ValueError('Mem0 或 Qdrant 版本与 P6 验证版本不符，已停止打开原生存储。')
        from personal_workbench.app_settings import AppSettings
        prefs=AppSettings(self.memory.settings);cfg=self.legacy.config()['mem0']
        runtime=prefs.runtime(cfg.get('model_profile_id'));embedding=prefs.profile(cfg.get('embedding_profile_id'),kind='embedding')
        signature=hashlib.sha256(json.dumps([embedding.provider,embedding.base_url,embedding.model]).encode()).hexdigest()
        pinned=self.store.meta('embedding')
        if pinned and pinned['signature']!=signature and not admin:raise ValueError('Mem0 嵌入配置已改变，请在存储与诊断中构建并切换新代次。')
        engine=Mem0Engine(self.memory.settings,runtime,embedding,sdk_factory=self.sdk_factory);engine.root=self.store.root
        if pinned:engine._dimensions=pinned['dimensions']
        elif self.sdk_factory:engine._dimensions=getattr(self.sdk_factory,'dimensions',3)
        else:engine.dimensions()
        if not pinned:self.store.set_meta('embedding',{'signature':signature,'dimensions':engine.dimensions(),'model':embedding.model,'provider':embedding.provider,'profile_id':embedding.id})
        # Prevent SDK logs from copying full model memory actions to process logs.
        import logging
        logging.getLogger('mem0.memory.main').setLevel(logging.CRITICAL)
        from .mem0_storage import Storage
        with engine.client(Storage(self).path()) as sdk:
            original_llm=sdk.llm
            from .mem0_usage import Meter
            sdk.llm=Meter(sdk.llm,self.store,'native_llm');sdk.embedding_model=Meter(sdk.embedding_model,self.store,'native_embedding')
            sdk._workbench_llm_kwargs={'extra_body':{'thinking':{'type':'disabled'}}} if runtime.provider=='deepseek' else {}
            sdk.config.custom_fact_extraction_prompt=EXTRACTION;sdk.config.custom_update_memory_prompt=UPDATE
            try:yield sdk
            finally:sdk.llm=original_llm

    def action(self,aid):
        with self.store.connect() as db:r=db.execute('SELECT * FROM actions WHERE id=?',(aid,)).fetchone()
        if not r:raise ValueError('原生操作账本不存在。')
        return dict(r)

    def verify_action(self,sdk,aid,repair=False):
        action=self.action(aid);op=self.store.operation(action['operation_id']);mid=action['memory_id']
        native=sdk.get(mid);payload=json.loads(action['payload']);old=json.loads(action['old'])
        changed=(native is None) if action['event']=='DELETE' else bool(native and native.get('metadata',{}).get('wb_action')==aid and native['memory']==payload['data'])
        history=sdk.history(mid);hits=[h for h in history if h.get('actor_id')=='wb:'+aid]
        if not changed:
            # Before-write failure is decidable only if native and history match baseline.
            untouched=(native is None and old is None) or (native is not None and old is not None and native['memory']==old.get('data') and native.get('metadata',{}).get('wb_action')==old.get('wb_action'))
            if repair and untouched and not hits:
                with self.store.connect() as db:db.execute("UPDATE actions SET state='not_applied',old=NULL,payload=NULL WHERE id=?",(aid,))
                return
            raise ValueError('原生对象与操作账本不一致，需要人工核对。')
        if not hits and repair:
            # Complete the missing SDK history through its original storage API.
            history_db=sdk.db.b.history if hasattr(sdk.db,'b') else sdk.db
            history_db.add_history(mid,old.get('data') if old else None,payload.get('data') if payload else None,action['event'],created_at=(payload or old or {}).get('created_at'),updated_at=(payload or {}).get('updated_at'),is_deleted=int(action['event']=='DELETE'),actor_id='wb:'+aid,role='user')
            hits=[h for h in sdk.history(mid) if h.get('actor_id')=='wb:'+aid]
        if len(hits)!=1 or hits[0]['event']!=action['event'] or hits[0]['old_memory']!=(old.get('data') if old else None) or hits[0]['new_memory']!=(payload.get('data') if payload else None):raise ValueError('原生历史尚未完整，不发布此操作。')
        with self.store.connect() as db:
            if db.execute("SELECT state FROM actions WHERE id=?",(aid,)).fetchone()[0]=='verified':return
            current=db.execute('SELECT * FROM controls WHERE id=?',(mid,)).fetchone()
            state='deleted' if action['event']=='DELETE' else current['state'] if current else 'active'
            locked=int(op['kind'] in ('manual_add','edit','import')) if op['kind']!='infer' else (current['locked'] if current else 0)
            if self.channel_kind=='event':locked=int(json.loads(op['frozen']).get('_event_locked',current['locked'] if current else False))
            db.execute('INSERT INTO controls VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,state=excluded.state,locked=excluded.locked,source_thread=excluded.source_thread,source_run=excluded.source_run,updated=excluded.updated',(mid,op['space_id'],current['version']+1 if current else 1,state,locked,op['thread_id'],op['run_id'],current['created'] if current else time.time(),time.time()))
            source_thread=(payload or {}).get('wb_source_thread',op['thread_id']);source_run=(payload or {}).get('wb_source_run',op['run_id'])
            if source_thread and source_run:db.execute('INSERT OR IGNORE INTO native_sources VALUES (?,?,?)',(mid,source_thread,source_run))
            db.execute("UPDATE actions SET state='verified',old=NULL,payload=NULL WHERE id=?",(aid,))
            if self.channel_kind=='procedure' and action['event']!='DELETE':
                from .mem0_procedures import Procedures
                Procedures(self).recorded(db,op,mid,payload)
            if self.channel_kind in ('ordinary','event') and action['event']=='UPDATE':
                from .mem0_context import Contexts
                Contexts(self).invalidate_memory(db,op['space_id'],mid,related_sources=self.channel_kind=='event')
            if op['kind'] in ('edit','event_edit'):
                # An explicit full replacement is independent evidence. Old
                # source dependencies must not invalidate this manual revision.
                db.execute('DELETE FROM native_sources WHERE memory_id=?',(mid,))

    def result(self,oid):
        op=self.store.operation(oid)
        with self.store.connect() as db:
            actions=[dict(r) for r in db.execute('SELECT id,memory_id,event,state FROM actions WHERE operation_id=?',(oid,))]
            interventions=db.execute('SELECT count(*) FROM interventions WHERE operation_id=?',(oid,)).fetchone()[0]
        return {'kind':self.channel_kind,'operation_key':[PROFILE,op['space_id'],self.channel_kind,oid],'operation_id':oid,'status':op['state'],'reason':op['reason'],'count':sum(a['state']=='verified' for a in actions),'actions':actions,'interventions':interventions}

    def execute(self,sdk,op,mid=None):
        boundary=WriteBoundary(self,sdk,op)
        infer=op['kind']=='infer'
        if infer:sdk.llm=NativeLLM(sdk.llm,op['source'],sdk._workbench_llm_kwargs)
        failed=False
        try:
            metadata={'user_id':op['space_id']}
            if op['kind'] in ('procedure_auto','procedure_generate'):
                from .mem0_procedures import ProcedureLLM,PROMPT,AGENT
                sdk.llm=ProcedureLLM(sdk.llm,sdk._workbench_llm_kwargs)
                sdk.add([{'role':'user','content':op['source']}],user_id=op['space_id'],agent_id=AGENT,memory_type='procedural_memory',metadata=metadata,prompt=PROMPT)
            elif op['kind']=='procedure_edit':
                from .mem0_procedures import AGENT
                metadata.update(agent_id=AGENT,memory_type='procedural_memory')
                if mid:sdk.update(mid,op['source'],metadata=metadata)
                else:sdk.add([{'role':'user','content':op['source']}],user_id=op['space_id'],agent_id=AGENT,metadata=metadata,infer=False)
            elif op['kind'] in ('event_auto','event_add','event_edit'):
                target=json.loads(op['frozen']).get('_target')
                if target:sdk.update(target,op['source'],metadata=metadata)
                else:sdk.add([{'role':'user','content':op['source']}],user_id=op['space_id'],metadata=metadata,infer=False)
            elif op['kind'] in ('infer','manual_add','import'):sdk.add([{'role':'user','content':op['source']}],user_id=op['space_id'],metadata=metadata,infer=infer)
            elif op['kind']=='edit':sdk.update(mid,op['source'],metadata=metadata)
            elif op['kind']=='delete':sdk.delete(mid)
            self.fault('after_response',op['id'])
            if boundary.failed or (infer and (sdk.llm.failed or sum(a['event']!='NONE' for a in sdk.llm.actions)!=boundary.attempts)):failed=True
        except Exception:failed=True
        with self.store.connect() as db:
            uncertain=db.execute("SELECT 1 FROM actions WHERE operation_id=? AND state='prepared'",(op['id'],)).fetchone()
            any_write=db.execute('SELECT 1 FROM actions WHERE operation_id=?',(op['id'],)).fetchone()
            if self.channel_kind in ('event','procedure') and not any_write:failed=True
        state='needs_reconcile' if uncertain or (failed and any_write) else 'failed' if failed else 'completed'
        self.store.finish(op['id'],state,'native_result_uncertain' if state=='needs_reconcile' else 'native_call_failed' if failed else '')
        return self.result(op['id'])

    def new_write(self,sid,kind,content='',mid=None,version=None,category=None):
        self.require_space(sid)
        if self.channel_kind in ('event','procedure') and kind!='delete':raise ValueError('请使用事件编辑接口。')
        if not self.ready:raise ValueError('请先将旧 Mem0 记忆迁移到原生存储。')
        if kind in ('manual_add','edit') and (not content.strip() or len(content)>1000):raise ValueError('记忆内容须为 1–1000 字符。')
        with self.serial():
            self.store.require_no_batch(sid)
            frozen={**self.frozen(sid),'_target':mid}
            if category is not None:
                from .mem0_channels import category as checked_category
                frozen['_category']=checked_category(category)
            if self.store.unresolved(sid):raise ValueError('此空间有结果待核对的操作，请先核对。')
            if mid:
                c=self.store.control(mid)
                if c['space_id']!=sid or c['version']!=version or c['state']=='deleted':raise ValueError('原生记忆版本或状态已变化，请刷新。')
            with self.client() as sdk:
                oid=self.store.start(sid,kind,content,frozen,run=uuid4().hex)
                try:
                    if kind=='delete':self.barrier(sdk,sid,mid)
                    result=self.execute(sdk,self.store.operation(oid),mid)
                except Exception:
                    self.store.finish(oid,'needs_reconcile','native_result_uncertain')
                    return self.result(oid)
                if kind=='delete':
                    try:
                        if result['status']=='completed':self.scrub(sdk,mid)
                        else:self.store.finish(oid,'needs_reconcile','delete_cleanup_pending')
                    except Exception:self.store.finish(oid,'needs_reconcile','delete_cleanup_pending')
                    result=self.result(oid)
                return result

    def barrier(self,sdk,sid,mid):
        record=sdk.get(mid)
        if not record:raise ValueError('原生记忆不存在，需先核对。')
        with self.registry.guard(),self.store.connect() as db:
            self.registry.require_active(PROFILE)
            c=self.store.control(mid)
            db.execute('INSERT OR IGNORE INTO tombstones VALUES (?,?,?,?,?,?)',(sid,mid,fingerprint(record['memory']),c['source_thread'],c['source_run'],time.time()))
            db.execute('INSERT OR IGNORE INTO blocked_hashes VALUES (?,?)',(sid,fingerprint(record['memory'])))
            for source in db.execute('SELECT thread_id,run_id FROM native_sources WHERE memory_id=?',(mid,)).fetchall():
                db.execute('INSERT OR IGNORE INTO source_barriers VALUES (?,?,?)',(sid,*source))
                db.execute("UPDATE operations SET state='cancelled',source='',reason='source_deleted' WHERE space_id=? AND thread_id=? AND run_id=? AND state IN ('pending','failed')",(sid,*source))
            db.execute("UPDATE controls SET state='deleted' WHERE id=?",(mid,))
            from .mem0_context import Contexts
            Contexts(self).invalidate_memory(db,sid,mid)

    def scrub(self,sdk,mid):
        history=sdk.db.b.history if hasattr(sdk.db,'b') else sdk.db
        with history._lock,history.connection:history.connection.execute('UPDATE history SET old_memory=NULL,new_memory=NULL WHERE memory_id=?',(mid,))
        with self.store.connect() as db:
            db.execute('UPDATE actions SET old=NULL,payload=NULL WHERE memory_id=?',(mid,))
            if self.channel_kind in ('event','procedure'):
                for op in db.execute('SELECT id,frozen FROM operations WHERE id IN (SELECT operation_id FROM actions WHERE memory_id=?)',(mid,)).fetchall():
                    f=json.loads(op['frozen']);f.pop('_event',None);f.pop('_event_quote',None);f.pop('_procedure',None)
                    db.execute('UPDATE operations SET frozen=? WHERE id=?',(json.dumps(f),op['id']))
            db.execute("UPDATE interventions SET proposal='',state='deleted' WHERE memory_id=?",(mid,))
            db.execute("UPDATE id_map SET history='[]' WHERE native_id=?",(mid,))
            for source in db.execute('SELECT thread_id,run_id FROM native_sources WHERE memory_id=?',(mid,)).fetchall():db.execute("UPDATE operations SET source='' WHERE thread_id=? AND run_id=?",tuple(source))

    def reconcile(self,oid):
        with self.serial():
            op=self.store.operation(oid);self.frozen(op['space_id'])
            if json.loads(op['frozen']).get('_batch'):raise ValueError('请在我的记忆中重试整组保存。')
            if op['state'] not in ('needs_reconcile','running'):raise ValueError('此操作不需要核对。')
            with self.client() as sdk:
                with self.store.connect() as db:actions=db.execute('SELECT id,state,memory_id,event FROM actions WHERE operation_id=?',(oid,)).fetchall()
                for action in actions:
                    if action['state']=='prepared':self.verify_action(sdk,action['id'],repair=True)
                    if action['event']=='DELETE' and sdk.get(action['memory_id']) is None:self.scrub(sdk,action['memory_id'])
                # Deletion has already erected a barrier. Finish this deterministic cleanup,
                # including the case where the earlier attempt stopped before vector deletion.
                if op['kind']=='delete':
                    mid=json.loads(op['frozen'])['_target']
                    if sdk.get(mid) is not None:
                        if self.store.control(mid)['state']!='deleted':self.barrier(sdk,op['space_id'],mid)
                        current={**self.frozen(op['space_id']),'_target':mid}
                        with self.store.connect() as db:db.execute("UPDATE operations SET state='running',frozen=? WHERE id=?",(json.dumps(current),oid))
                        result=self.execute(sdk,self.store.operation(oid),mid)
                        if result['status']!='completed':
                            self.store.finish(oid,'needs_reconcile','delete_cleanup_pending')
                            return self.result(oid)
                    try:self.scrub(sdk,mid)
                    except Exception:
                        self.store.finish(oid,'needs_reconcile','delete_cleanup_pending')
                        raise ValueError('删除清理尚未完成，请再次核对。') from None
                # Never call add again: missing planned actions remain unexecuted.
                self.store.finish(oid,'reconciled','verified_existing_actions_no_replay')
            return self.result(oid)

    def manage_state(self,sid,mid,version,state,locked):
        if state not in ('active','archived'):raise ValueError('无效的原生记忆状态。')
        with self.serial(),self.client() as sdk,self.registry.guard(),self.store.connect() as db:
            self.frozen(sid);self.store.require_no_batch(sid);c=self.store.control(mid)
            if not sdk.get(mid):raise ValueError('原生对象不可用，不能更改状态。')
            if c['space_id']!=sid or c['version']!=version or c['state']=='deleted':raise ValueError('原生记忆版本或状态已变化，请刷新。')
            if self.store.unresolved(sid):raise ValueError('此空间有结果待核对的操作，请先核对。')
            db.execute('UPDATE controls SET state=?,locked=?,version=version+1,updated=? WHERE id=?',(state,int(locked),time.time(),mid))
            if state=='archived':
                from .mem0_context import Contexts
                Contexts(self).invalidate_memory(db,sid,mid)
        return {'saved':True}

    def visible(self,sdk,sid):
        self.store.require_no_batch(sid)
        if self.store.unresolved(sid):raise ValueError('Mem0 原生操作结果待核对，当前空间暂停召回和新写入。')
        with self.store.connect() as db:controls={r['id']:dict(r) for r in db.execute('SELECT * FROM controls WHERE space_id=?',(sid,))}
        native=sdk.get_all(user_id=sid,limit=max(1,len(controls)+1))['results']
        result=[]
        for row in native:
            control=controls.get(row['id'])
            if not control or control['state']=='deleted':continue
            result.append({**row,**control,'content':row['memory'],'source_quote':row.get('metadata',{}).get('wb_source_quote',''),'category':row.get('metadata',{}).get('wb_category','other'),'kind':self.channel_kind,'object_key':[PROFILE,sid,self.channel_kind,row['id']]})
        return result

    def listing(self,sid,query='',status='active',page=1):
        self.require_space(sid)
        if not self.ready:return {'items':[],'total':0,'page':page,'migration_required':True}
        with self.serial(),self.client() as sdk:
            rows=[r for r in self.visible(sdk,sid) if (status=='all' or r['state']==status) and query.casefold() in r['memory'].casefold()]
        rows.sort(key=lambda r:r['updated'],reverse=True)
        return {'items':rows[(page-1)*30:page*30],'total':len(rows),'page':page,'migration_required':False}

    def detail(self,sid,mid):
        self.require_space(sid);self.store.require_no_batch(sid);control=self.store.control(mid)
        if control['space_id']!=sid:raise ValueError('原生记忆不属于此空间。')
        with self.serial(),self.client() as sdk:
            native=None if control['state']=='deleted' else sdk.get(mid)
            history=sdk.history(mid)
        with self.store.connect() as db:
            mapped=db.execute('SELECT * FROM id_map WHERE native_id=?',(mid,)).fetchone()
            actions=[dict(r) for r in db.execute('SELECT id,operation_id,event,state,created FROM actions WHERE memory_id=? ORDER BY created DESC',(mid,))]
        return {'control':control,'native':native,'native_history':history,'imported_history':json.loads(mapped['history']) if mapped else [],'legacy_id':mapped['legacy_id'] if mapped else None,'actions':actions,'needs_reconcile':self.store.unresolved(sid)}

    def search(self,frozen,query):
        self.check_frozen(frozen);sid=frozen['space_id'];self.require_space(sid)
        with self.serial(),self.client() as sdk:
            all_rows=self.visible(sdk,sid)
            rows={r['id']:r for r in all_rows if r['state']=='active' and self.sources_valid(r['id'])}
            if not rows:return []
            hits=sdk.search(query,user_id=sid,limit=max(1,len(all_rows)),threshold=frozen['mem0']['similarity_threshold'],rerank=False)['results']
            self.check_frozen(frozen)
            return [{**rows[h['id']],'score':h.get('score'),'memory_type':'fact','profile_key':'','scope_kind':'personal','scope_id':'personal','conditions':'','category':rows[h['id']].get('category','other')} for h in hits if h['id'] in rows][:frozen['recall_limit']]

    def sources_valid(self,mid):
        with self.store.connect() as db:
            for table in ('source_barriers','context_exclusions'):
                if db.execute(f'SELECT 1 FROM native_sources s JOIN {table} b ON s.thread_id=b.thread_id AND s.run_id=b.run_id JOIN controls c ON c.id=s.memory_id AND c.space_id=b.space_id WHERE s.memory_id=?',(mid,)).fetchone():return False
        return True

    def enqueue(self,frozen,text,thread,run,await_answer=False):
        if not frozen or frozen.get('engine')!='mem0' or not frozen.get('learn_memories') or not frozen.get('enabled',True):return {'status':'disabled','count':0}
        with self.registry.guard():
            self.check_frozen(frozen)
            with self.store.connect() as db:
                if db.execute('SELECT 1 FROM context_exclusions WHERE space_id=? AND thread_id=? AND run_id=?',(frozen['space_id'],thread,run)).fetchone():return {'status':'skipped','reason':'context_source_excluded','count':0}
                if db.execute('SELECT 1 FROM source_barriers WHERE space_id=? AND thread_id=? AND run_id=?',(frozen['space_id'],thread,run)).fetchone():return {'status':'skipped','reason':'source_deleted','count':0}
            excluded=bool(EXCLUDED.search(text));oid=self.store.start(frozen['space_id'],'infer','' if excluded else text,{**frozen,'_infer':True},thread,run,'skipped' if excluded else 'awaiting_answer' if await_answer else 'pending')
        if self.channel_kind=='ordinary':
            from .mem0_events import Events
            Events(self).enqueue(frozen,text,thread,run,await_answer)
            from .mem0_procedures import Procedures
            Procedures(self).enqueue(frozen,text,thread,run,await_answer)
        result=self.result(oid);return {**result,'status':'queued' if result['status']=='pending' else result['status']}

    def release(self,oid,completed=True,messages=None):
        with self.registry.guard(),self.store.connect() as db:
            op=self.store.operation(oid)
            if op['state']=='awaiting_answer':
                if completed:db.execute("UPDATE operations SET state='pending',updated=? WHERE id=?",(time.time(),oid))
                else:db.execute("UPDATE operations SET state='cancelled',reason='answer_not_completed',source='' WHERE id=?",(oid,))
        if self.channel_kind=='ordinary':
            from .mem0_events import Events
            Events(self).release(json.loads(op['frozen']),op['thread_id'],op['run_id'],completed,messages)
            from .mem0_procedures import Procedures
            Procedures(self).release(json.loads(op['frozen']),op['thread_id'],op['run_id'],completed)
        return self.result(oid)

    def recover_answers(self):
        # Only completion metadata for sources registered before the new answer.
        with self.store.connect() as db:waiting=db.execute("SELECT id,thread_id,run_id FROM operations WHERE state='awaiting_answer'").fetchall()
        from personal_workbench.workflows.memory import parent_outcome,delivery_receipt
        for row in waiting:
            parent=parent_outcome(self.registry.root,row['thread_id'],row['run_id'])
            if parent and parent['terminal']:
                self.release(row['id'],completed=parent['accepted'],messages=delivery_receipt(parent['artifact']) if parent['accepted'] else [])
                continue
            for filename,query in [('app.sqlite',"SELECT 1 FROM sessions WHERE id=? AND status='completed' AND json_extract(skill_snapshot,'$.run_id')=?"),('web.sqlite',"SELECT 1 FROM jobs WHERE thread_id=? AND status='completed' AND json_extract(snapshot,'$.run_id')=? LIMIT 1")]:
                path=self.registry.root/filename
                if path.exists():
                    with sqlite3.connect(f'file:{path}?mode=ro',uri=True) as db:done=db.execute(query,(row['thread_id'],row['run_id'])).fetchone()
                    if done:self.release(row['id']);break

    def run_once(self):
        if not self.ready or self.registry.state()['active_profile_id']!=PROFILE or not (self.legacy.config().get('enabled',True) and self.legacy.config()['learn_memories']):return False
        with self.serial():
            with self.store.connect() as db:
                op=db.execute("""SELECT * FROM operations o WHERE kind='infer' AND state='pending'
                 AND NOT EXISTS(SELECT 1 FROM category_batches b WHERE b.space_id=o.space_id AND b.state!='completed')
                 AND NOT EXISTS(SELECT 1 FROM operations p WHERE p.space_id=o.space_id AND (p.state IN ('running','needs_reconcile') OR (p.thread_id=o.thread_id AND p.created<o.created AND p.state NOT IN ('completed','cancelled','skipped','reconciled')))) ORDER BY created LIMIT 1""").fetchone()
            if not op:return False
            op=dict(op);frozen=json.loads(op['frozen'])
            try:self.check_frozen(frozen)
            except ValueError:self.store.finish(op['id'],'skipped','activation_or_config_changed');return True
            with self.store.connect() as db:db.execute("UPDATE operations SET state='running',updated=? WHERE id=? AND state='pending'",(time.time(),op['id']))
            try:
                with self.client() as sdk:self.execute(sdk,self.store.operation(op['id']))
            except Exception:self.store.finish(op['id'],'needs_reconcile','native_open_or_result_failed')
        return True

    def job_action(self,oid,action):
        op=self.store.operation(oid)
        if json.loads(op['frozen']).get('_batch'):raise ValueError('请在我的记忆中重试整组保存。')
        with self.registry.guard():
            self.frozen(op['space_id'])
            if action=='cancel' and op['state'] in ('pending','failed','awaiting_answer'):
                self.store.finish(oid,'cancelled','user_cancelled')
            elif action=='retry' and op['state']=='failed' and op['kind']=='infer':
                with self.store.connect() as db:
                    if db.execute('SELECT 1 FROM actions WHERE operation_id=?',(oid,)).fetchone():raise ValueError('存在原生写入记录，不能重新执行 add。')
                    if db.execute('SELECT 1 FROM source_barriers WHERE space_id=? AND thread_id=? AND run_id=?',(op['space_id'],op['thread_id'],op['run_id'])).fetchone():raise ValueError('来源已删除，不能重放。')
                    db.execute("UPDATE operations SET state='pending',reason='',frozen=? WHERE id=?",(json.dumps({**self.frozen(op['space_id']),'_infer':True}),oid))
            else:raise ValueError('此任务状态不支持该操作。')
        return self.result(oid)

    def dashboard(self,sid,page=1):
        self.require_space(sid)
        with self.store.connect() as db:
            ops=[dict(r) for r in db.execute('SELECT id,kind,state,reason,thread_id,run_id,created,updated FROM operations WHERE space_id=? ORDER BY created DESC LIMIT 30 OFFSET ?',(sid,(page-1)*30))]
            for op in ops:op['actions']=[dict(a) for a in db.execute('SELECT memory_id,event,state FROM actions WHERE operation_id=?',(op['id'],))]
            pending=[dict(r) for r in db.execute("SELECT * FROM interventions WHERE space_id=? AND state='pending' ORDER BY created DESC LIMIT 30 OFFSET ?",(sid,(page-1)*30))]
            counts=dict(db.execute('SELECT state,count(*) FROM controls WHERE space_id=? GROUP BY state',(sid,)))
            mapped=db.execute('SELECT count(*) FROM id_map WHERE space_id=?',(sid,)).fetchone()[0]
            cursors=[dict(r) for r in db.execute('SELECT * FROM cursors WHERE space_id=?',(sid,))]
        with self.legacy.connect() as db:
            legacy_count=db.execute('SELECT count(*) FROM memories WHERE space_id=? AND deleted=0',(sid,)).fetchone()[0]
            migration_plan={'items':db.execute('SELECT count(*) FROM memories WHERE deleted=0').fetchone()[0],'deleted':db.execute('SELECT count(*) FROM memories WHERE deleted=1').fetchone()[0],'spaces':db.execute('SELECT count(*) FROM spaces').fetchone()[0]}
        from .mem0_channels import capabilities
        return {'capabilities':capabilities(),'ready':self.ready,'operations':ops,'interventions':pending,'counts':counts,'mapped':mapped,'legacy_count':legacy_count,'migration_plan':migration_plan,'embedding':self.store.meta('embedding'),'cursors':cursors,'sdk':'1.0.11','context':self.legacy.config()['mem0'].get('context_strategy','recent'),'paused':self.registry.state()['active_profile_id']!=PROFILE,'page':page}

    def dismiss(self,iid):
        with self.registry.guard(),self.store.connect() as db:
            self.registry.require_active(PROFILE)
            db.execute("UPDATE interventions SET state='dismissed' WHERE id=? AND state='pending'",(iid,))
        return {'saved':True}

    def migrate(self):
        """Resumable direct import. Legacy store remains an explicitly frozen archive."""
        with self.serial():
            self.registry.require_active(PROFILE)
            if self.ready:return {'published':True,'already_done':True}
            with self.registry.guard():self.store.set_meta('migration_started',True)
            with self.legacy.connect() as db:
                rows=[dict(r) for r in db.execute('SELECT * FROM memories ORDER BY id')]
                hashes=[tuple(r) for r in db.execute('SELECT space_id,hash FROM blocked_hashes')]
            with self.store.connect() as db:
                for sid,digest in hashes:db.execute('INSERT OR IGNORE INTO blocked_hashes VALUES (?,?)',(sid,digest))
            imported=0
            for row in rows:
                with self.store.connect() as db:
                    if db.execute('SELECT 1 FROM id_map WHERE legacy_id=?',(row['id'],)).fetchone():continue
                    if row['deleted']:
                        if row['source_thread'] and row['source_run']:db.execute('INSERT OR IGNORE INTO source_barriers VALUES (?,?,?)',(row['space_id'],row['source_thread'],row['source_run']))
                        db.execute('INSERT INTO id_map VALUES (?,?,?,?,?)',(row['id'],None,row['space_id'],row['version'],'[]'));continue
                frozen={**self.frozen(row['space_id']),'_import_quote':row['source_quote'],'_source_thread':row['source_thread'],'_source_run':row['source_run']}
                with self.store.connect() as db:
                    if db.execute("SELECT 1 FROM actions a JOIN operations o ON a.operation_id=o.id WHERE o.kind='import' AND o.run_id=? AND a.state='prepared'",(row['id'],)).fetchone():raise ValueError('迁移操作结果待核对，不能重复导入。')
                oid=self.store.start(row['space_id'],'import',row['content'],frozen,'migration',row['id'])
                op=self.store.operation(oid)
                if op['state'] in ('needs_reconcile',):raise ValueError('迁移操作结果待核对，请先在摄取任务中核对，不能重复导入。')
                with self.store.connect() as db:actions=db.execute("SELECT memory_id FROM actions WHERE operation_id=? AND state='verified'",(oid,)).fetchall()
                if not actions:
                    if op['state'] not in ('running','failed','reconciled'):raise ValueError('迁移记录需要人工核对。')
                    with self.store.connect() as db:
                        if db.execute("SELECT 1 FROM operations WHERE space_id=? AND id!=? AND state IN ('running','needs_reconcile')",(row['space_id'],oid)).fetchone():raise ValueError('此空间有待核对操作。')
                    with self.store.connect() as db:db.execute("UPDATE operations SET state='running',source=?,frozen=? WHERE id=?",(row['content'],json.dumps(frozen),oid))
                    with self.client() as sdk:result=self.execute(sdk,self.store.operation(oid))
                    if result['status']!='completed':raise ValueError('迁移尚未完成，请先核对失败操作。')
                    actions=[a for a in result['actions'] if a['state']=='verified']
                if len(actions)!=1:raise ValueError('原生导入数量异常，未发布迁移。')
                mid=actions[0]['memory_id']
                with self.legacy.connect() as db:history=[dict(h) for h in db.execute('SELECT * FROM memory_changes WHERE memory_id=? ORDER BY id',(row['id'],))]
                with self.store.connect() as db:
                    db.execute('UPDATE controls SET version=?,locked=?,state=?,source_thread=?,source_run=? WHERE id=?',(row['version'],int(row['locked'] or row['manual']),row['status'],row['source_thread'],row['source_run'],mid))
                    db.execute('INSERT INTO id_map VALUES (?,?,?,?,?)',(row['id'],mid,row['space_id'],row['version'],json.dumps(history,ensure_ascii=False)))
                imported+=1
            self.store.set_meta('published',True)
        return {'published':True,'imported':imported,'legacy_retained':True}


class NativeWorker:
    def __init__(self,native):self.native=native;self.stop=threading.Event();self.thread=None
    def start(self):
        if self.thread:return
        self.thread=threading.Thread(target=self.loop,name='mem0-native-worker',daemon=True);self.thread.start()
    def loop(self):
        with (self.native.store.root/'worker.lock').open('a') as lock:
            while not self.stop.is_set():
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:self.stop.wait(1)
            if self.stop.is_set():return
            with self.native.serial(),self.native.store.connect() as db:
                db.execute("UPDATE operations SET state='needs_reconcile',reason='process_interrupted' WHERE state='running'")
            with self.native.serial():
                for kind in ('event','procedure'):
                    event=self.native.channel(kind)
                    with event.store.connect() as db:db.execute("UPDATE operations SET state='needs_reconcile',reason='process_interrupted' WHERE state='running'")
            while not self.stop.is_set():
                try:
                    self.native.recover_answers()
                    from .mem0_events import Events
                    events=Events(self.native);events.recover_answers();events.run_once()
                    from .mem0_procedures import Procedures
                    procedures=Procedures(self.native);procedures.recover_answers();procedures.run_once()
                    if self.native.run_once():continue
                except Exception:pass
                self.stop.wait(1)
    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=1)
