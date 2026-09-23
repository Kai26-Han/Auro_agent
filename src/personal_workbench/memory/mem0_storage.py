"""Verified native generations and local, instance-only snapshot/restore.

Embedding migration re-embeds native payloads with their original IDs. It never
calls inference and never rebuilds from the legacy memory table.
"""
import hashlib,json,shutil,sqlite3,time,zipfile
from pathlib import Path
from uuid import uuid4
from contextlib import contextmanager
from personal_workbench.app_settings import AppSettings
from .mem0_engine import Mem0Engine
from .mem0_native_store import PROFILE

def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
def signature(profile):return hashlib.sha256(json.dumps([profile.provider,profile.base_url,profile.model]).encode()).hexdigest()
def safe_profile(profile):return profile.model_dump(exclude={'api_key'})
def snapshot_sqlite(source,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    src=sqlite3.connect(f'file:{source}?mode=ro',uri=True);dest=sqlite3.connect(target)
    try:
        src.backup(dest);dest.commit();dest.execute('PRAGMA journal_mode=DELETE')
        if dest.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('备份数据库完整性校验失败。')
    finally:src.close();dest.close()

def copy_tree(source,target):
    target.mkdir(parents=True,exist_ok=True)
    if not source.exists():return
    for path in source.rglob('*'):
        if path.is_symlink():raise ValueError('原生存储中发现符号链接，停止复制。')
        if not path.is_file() or path.name.endswith(('-wal','-shm','-journal','.lock')) or path.name=='.lock':continue
        out=target/path.relative_to(source);out.parent.mkdir(parents=True,exist_ok=True)
        with path.open('rb') as f:is_sqlite=f.read(16)==b'SQLite format 3\x00'
        if is_sqlite:snapshot_sqlite(path,out)
        else:shutil.copy2(path,out)
def tree_hash(root):
    return digest([(str(p.relative_to(root)),hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(root.rglob('*')) if p.is_file() and p.name not in ('manifest.json','snapshot.zip')])

class Storage:
    def __init__(self,native):self.n=native;self.store=native.store;self.root=self.store.root
    def event_storage(self):
        return Storage(self.n.channel('event')) if self.n.channel_kind=='ordinary' else None
    def substores(self):
        return [Storage(self.n.channel(kind)) for kind in ('event','procedure')] if self.n.channel_kind=='ordinary' else []
    @property
    def active(self):return self.store.meta('active_generation') or 'legacy'
    def path(self,generation=None):
        generation=generation or self.active
        if generation=='legacy':return self.root/'storage'
        if len(generation)!=32 or any(c not in '0123456789abcdef' for c in generation):raise ValueError('无效的存储代次。')
        return self.root/'generations'/generation
    def ensure_idle(self):
        for sub in self.substores():sub.ensure_idle()
        with self.store.connect() as db:
            if db.execute("SELECT 1 FROM category_batches WHERE state!='completed'").fetchone():raise ValueError('请先完成分类保存，再管理存储。')
            if db.execute("SELECT 1 FROM operations WHERE state IN ('running','needs_reconcile')").fetchone():raise ValueError('请先核对所有原生操作，再管理存储。')
    def baseline(self):
        with self.store.connect() as db:return digest({'generation':self.active,'sources':[tuple(r) for r in db.execute('SELECT * FROM native_sources ORDER BY memory_id,thread_id,run_id')],'source_barriers':[tuple(r) for r in db.execute('SELECT * FROM source_barriers ORDER BY space_id,thread_id,run_id')],'exclusions':[tuple(r) for r in db.execute('SELECT * FROM context_exclusions ORDER BY space_id,thread_id,turn_id')],'controls':[tuple(r) for r in db.execute('SELECT * FROM controls ORDER BY id')],'actions':[tuple(r) for r in db.execute('SELECT id,state FROM actions ORDER BY id')],'channels':{sub.n.channel_kind:sub.baseline() for sub in self.substores()},'methods':[tuple(r) for r in db.execute('SELECT * FROM methods ORDER BY id')],'revisions':[tuple(r) for r in db.execute('SELECT * FROM method_revisions ORDER BY native_id')]})
    def restore_state(self):
        with self.store.connect() as db:
            context=[tuple(r) for r in db.execute('SELECT thread_id,version FROM contexts ORDER BY thread_id')]
            barriers=[tuple(r) for r in db.execute('SELECT * FROM context_barriers ORDER BY space_id,thread_id')]
        with self.n.legacy.connect() as db:revision=db.execute('SELECT revision FROM config WHERE id=1').fetchone()[0]
        return digest({'native':self.baseline(),'generation':self.active,'revision':revision,'contexts':context,'barriers':barriers})
    def native_rows(self,sdk):
        # SDK list is finite; verify exact backend count to avoid silent truncation.
        count=sdk.vector_store.client.count(collection_name=sdk.vector_store.collection_name,exact=True).count
        rows=sdk.vector_store.list(limit=max(1,count+1))[0]
        if len(rows)!=count:raise ValueError('原生库未完整读取，停止迁移或备份。')
        return {r.id:r.payload for r in rows}
    def coverage(self,sdk):
        native=self.native_rows(sdk)
        with self.store.connect() as db:controls={r['id']:dict(r) for r in db.execute('SELECT * FROM controls')}
        expected={k for k,v in controls.items() if v['state']!='deleted'}
        missing=expected-native.keys();extra=native.keys()-expected
        wrong=[mid for mid in expected & native.keys() if native[mid].get('user_id')!=controls[mid]['space_id']]
        return {'expected':len(expected),'present':len(native),'covered':len(expected & native.keys()),'missing':len(missing),'unexpected':len(extra),'wrong_scope':len(wrong),'complete':not (missing or extra or wrong)}
    @contextmanager
    def target_client(self,generation,profile,dimensions):
        prefs=AppSettings(self.n.memory.settings);runtime=prefs.runtime(self.n.legacy.config()['mem0'].get('model_profile_id'))
        engine=Mem0Engine(self.n.memory.settings,runtime,profile,sdk_factory=self.n.sdk_factory);engine.root=self.root;engine._dimensions=dimensions
        with engine.client(self.path(generation)) as sdk:yield sdk
    def generation(self,gid):
        with self.store.connect() as db:r=db.execute('SELECT * FROM generations WHERE id=?',(gid,)).fetchone()
        if not r:raise ValueError('存储代次不存在。')
        return dict(r)
    def start(self,profile_id):
        profile=AppSettings(self.n.memory.settings).profile(profile_id,kind='embedding')
        with self.n.serial(),self.n.registry.guard():
            self.n.registry.require_active(PROFILE);self.ensure_idle()
            if not self.n.ready:raise ValueError('请先完成旧版记忆迁移。')
            with self.store.connect() as db:
                if db.execute("SELECT 1 FROM generations WHERE state IN ('building','ready')").fetchone():raise ValueError('已有未完成代次，请继续或取消。')
            with self.n.client(admin=True) as sdk:
                if not self.coverage(sdk)['complete']:raise ValueError('原生索引覆盖不完整，不能开始迁移。')
                total=len(self.native_rows(sdk))
            for events in self.substores():
                with events.n.client(admin=True) as sdk:
                    if not events.coverage(sdk)['complete']:raise ValueError('事件索引覆盖不完整。')
                    total+=len(events.native_rows(sdk))
            if self.n.sdk_factory:dimensions=getattr(self.n.sdk_factory,'dimensions',3)
            else:
                from personal_workbench.rag_embedding import EndpointEmbedding
                dimensions=len(EndpointEmbedding(profile.embedding()).get_query_embedding('dimension check'))
            gid=uuid4().hex
            with self.store.connect() as db:db.execute('INSERT INTO generations VALUES (?,?,?,?,?,?,?,?,?,?)',(gid,'building',self.active,json.dumps(safe_profile(profile)),self.baseline(),0,total,dimensions,'',time.time()))
            return self.generation(gid)
    def step(self,gid,batch=16):
        with self.n.serial():
            self.n.registry.require_active(PROFILE);self.ensure_idle();gen=self.generation(gid)
            if gen['state'] not in ('building','failed'):raise ValueError('此代次不能继续构建。')
            if gen['source']!=self.active or gen['baseline']!=self.baseline():raise ValueError('原生记忆已变化，请取消并重新创建迁移代次。')
            profile=AppSettings(self.n.memory.settings).profile(json.loads(gen['target'])['id'],kind='embedding')
            if safe_profile(profile)!=json.loads(gen['target']):raise ValueError('目标嵌入配置已改变，请重新创建代次。')
            frozen=self.n.frozen(next(s['id'] for s in self.n.legacy.spaces()))
            try:
                with self.n.client(admin=True) as old,self.target_client(gid,profile,gen['dimensions']) as target:
                    rows=self.native_rows(old)
                    with self.store.connect() as db:done={r[0] for r in db.execute('SELECT memory_id FROM generation_items WHERE generation=?',(gid,))}
                    for mid in sorted(rows.keys()-done)[:batch]:
                        payload=rows[mid];start=time.time()
                        try:vector=target.embedding_model.embed(payload['data'],'add')
                        except Exception:
                            self.usage('migration_embedding',start,True);raise
                        self.usage('migration_embedding',start,False)
                        if len(vector)!=gen['dimensions']:raise ValueError('嵌入维度与目标代次不符。')
                        with self.n.registry.guard():
                            self.n.check_frozen(frozen)
                            target.vector_store.insert(vectors=[vector],ids=[mid],payloads=[payload])
                            self.n.fault('generation_after_vector',mid)
                            written=target.vector_store.get(vector_id=mid)
                            if not written or written.payload!=payload:raise ValueError('目标原生对象校验失败。')
                            with self.store.connect() as db:db.execute('INSERT OR IGNORE INTO generation_items VALUES (?,?)',(gid,mid))
                    target_rows=self.native_rows(target)
                    complete=target_rows==rows
                    if len(target_rows)>len(rows):raise ValueError('目标代次出现未知对象。')
                for events in self.substores():
                    channel=events.n.channel_kind
                    with events.n.client(admin=True) as old,events.target_client(gid,profile,gen['dimensions']) as target:
                        event_rows=events.native_rows(old)
                        with self.store.connect() as db:done={r[0] for r in db.execute('SELECT memory_id FROM generation_items WHERE generation=?',(gid,))}
                        for mid in [m for m in sorted(event_rows) if channel+':'+m not in done][:batch]:
                            vector=target.embedding_model.embed(event_rows[mid]['data'],'add')
                            if len(vector)!=gen['dimensions']:raise ValueError('事件嵌入维度不符。')
                            with self.n.registry.guard():
                                self.n.check_frozen(frozen)
                                target.vector_store.insert(vectors=[vector],ids=[mid],payloads=[event_rows[mid]])
                                if target.vector_store.get(vector_id=mid).payload!=event_rows[mid]:raise ValueError('事件目标正文校验失败。')
                                with self.store.connect() as db:db.execute('INSERT OR IGNORE INTO generation_items VALUES (?,?)',(gid,channel+':'+mid))
                        complete=complete and events.native_rows(target)==event_rows
                with self.store.connect() as db:db.execute('UPDATE generations SET state=?,done=(SELECT count(*) FROM generation_items WHERE generation=?),error=\'\' WHERE id=?',('ready' if complete else 'building',gid,gid))
            except Exception:
                with self.store.connect() as db:db.execute("UPDATE generations SET state='failed',error='build_failed' WHERE id=?",(gid,))
                raise ValueError('代次构建未完成，旧库未切换；请检查嵌入连接后继续。') from None
            return self.generation(gid)
    def usage(self,kind,start,failed):
        with self.store.connect() as db:db.execute('INSERT INTO native_usage VALUES (?,1,0,1,?,?,?)',(kind,int(failed),time.time()-start,time.time()))
    def publish(self,gid):
        with self.n.serial(),self.n.registry.guard():
            self.n.registry.require_active(PROFILE);self.ensure_idle();gen=self.generation(gid)
            if gen['state']!='ready' or gen['source']!=self.active or gen['baseline']!=self.baseline():raise ValueError('代次尚未就绪，或原生记忆已变化。')
            profile=AppSettings(self.n.memory.settings).profile(json.loads(gen['target'])['id'],kind='embedding')
            if safe_profile(profile)!=json.loads(gen['target']):raise ValueError('目标嵌入配置已改变。')
            with self.n.client(admin=True) as old,self.target_client(gid,profile,gen['dimensions']) as target:
                if self.native_rows(old)!=self.native_rows(target):raise ValueError('目标代次覆盖或正文校验失败。')
            for events in self.substores():
                with events.n.client(admin=True) as old,events.target_client(gid,profile,gen['dimensions']) as target:
                    if events.native_rows(old)!=events.native_rows(target):raise ValueError('事件代次校验失败。')
                snapshot_sqlite(events.path()/'history.sqlite',events.path(gid)/'history.sqlite')
            # Preserve native history byte-for-logical-row; never fabricate import events.
            snapshot_sqlite(self.path()/'history.sqlite',self.path(gid)/'history.sqlite')
            self.n.fault('generation_before_publish',gid)
            pin={'signature':signature(profile),'dimensions':gen['dimensions'],'model':profile.model,'provider':profile.provider,'profile_id':profile.id}
            self.commit_pointer(gid,pin,profile.id)
            return {'published':True,'generation':gid}
    def commit_pointer(self,gid,pin,profile_id):
        # Rollback-journal SQLite transaction spans the pointer and engine config.
        with self.store.connect() as db:
            db.execute('ATTACH DATABASE ? AS engine',(str(self.n.legacy.path),))
            subs=self.substores()
            for sub in subs:db.execute('ATTACH DATABASE ? AS channel_'+sub.n.channel_kind,(str(sub.store.path),))
            db.execute('BEGIN IMMEDIATE')
            config=json.loads(db.execute('SELECT value FROM engine.config WHERE id=1').fetchone()[0]);config['mem0']['embedding_profile_id']=profile_id
            db.execute('UPDATE engine.config SET value=?,revision=revision+1 WHERE id=1',(json.dumps(config),))
            for key,value in [('active_generation',gid),('embedding',pin)]:db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,json.dumps(value)))
            db.execute("UPDATE generations SET state='retired' WHERE state='active'")
            db.execute("UPDATE generations SET state='active' WHERE id=?",(gid,))
            for sub in subs:
                for key,value in [('active_generation',gid),('embedding',pin)]:db.execute('INSERT OR REPLACE INTO channel_'+sub.n.channel_kind+'.meta VALUES (?,?)',(key,json.dumps(value)))
            self.n.fault('pointer_before_commit',gid)
    def cancel(self,gid):
        with self.n.serial(),self.n.registry.guard(),self.store.connect() as db:
            self.n.registry.require_active(PROFILE)
            if self.active==gid:raise ValueError('不能取消当前正式代次。')
            db.execute("UPDATE generations SET state='cancelled' WHERE id=? AND state IN ('building','failed','ready')",(gid,))
        return {'cancelled':True}
    def diagnostics(self):
        with self.store.connect() as db:
            gens=[dict(r) for r in db.execute('SELECT id,state,source,done,total,dimensions,error,created FROM generations ORDER BY created DESC LIMIT 30')]
            usage=[dict(r) for r in db.execute('SELECT kind,sum(calls) calls,sum(tokens) tokens,sum(unknown) unknown,sum(failed) failed,sum(seconds) seconds FROM native_usage GROUP BY kind')]
            backups=[dict(r) for r in db.execute("SELECT id,state,created,items FROM backups WHERE state='ready' ORDER BY created DESC LIMIT 30")]
            queue=dict(db.execute('SELECT state,count(*) FROM operations GROUP BY state'))
        return {'generation':self.active,'embedding':self.store.meta('embedding'),'generations':gens,'usage':usage,'backups':backups,'queue':queue,'context_strategy':self.n.legacy.config()['mem0'].get('context_strategy','recent')}
    def check(self):
        if not self.n.ready:raise ValueError('请先完成旧版记忆迁移。')
        with self.n.serial(),self.n.client(admin=True) as sdk:return self.coverage(sdk)
    def backup(self):
        with self.n.serial(),self.n.registry.guard():
            self.n.registry.require_active(PROFILE);self.ensure_idle()
            if not self.n.ready:raise ValueError('请先完成旧版记忆迁移。')
            with self.n.client(admin=True) as sdk:
                coverage=self.coverage(sdk)
                if not coverage['complete']:raise ValueError('原生覆盖不完整，请先核对，不能创建可恢复快照。')
            bid=uuid4().hex;path=self.root/'backups'/bid;path.mkdir(parents=True,mode=0o700)
            copy_tree(self.path(),path/'storage')
            snapshot_sqlite(self.store.path,path/'management.sqlite');snapshot_sqlite(self.n.legacy.path,path/'engine.sqlite')
            channels={}
            for events in self.substores():
                with events.n.client(admin=True) as sdk:
                    check=events.coverage(sdk)
                    if not check['complete']:raise ValueError('事件覆盖不完整。')
                kind=events.n.channel_kind
                copy_tree(events.path(),path/(kind+'-storage'));snapshot_sqlite(events.store.path,path/(kind+'-management.sqlite'))
                channels[kind]={'embedding':events.store.meta('embedding'),'items':check['present']}
            manifest={'channels':channels,'format':'mem0-workbench-p7-v1','profile':PROFILE,'sdk':'1.0.11','embedding':self.store.meta('embedding'),'generation':self.active,'created':time.time(),'items':coverage['present'],'digest':tree_hash(path)}
            (path/'manifest.json').write_text(json.dumps(manifest))
            with zipfile.ZipFile(path/'snapshot.zip','w',zipfile.ZIP_DEFLATED) as archive:
                for item in sorted(path.rglob('*')):
                    if item.is_file() and item.name!='snapshot.zip':archive.write(item,item.relative_to(path))
            registered=digest({'tree':manifest['digest'],'manifest':manifest,'archive':hashlib.sha256((path/'snapshot.zip').read_bytes()).hexdigest()})
            with self.store.connect() as db:db.execute('INSERT INTO backups VALUES (?,?,?,?,?)',(bid,'ready',registered,manifest['created'],manifest['items']))
            return {'id':bid,**manifest}
    def backup_path(self,bid):
        if len(bid)!=32 or any(c not in '0123456789abcdef' for c in bid):raise ValueError('无效备份 ID。')
        path=self.root/'backups'/bid
        if path.is_symlink() or not (path/'manifest.json').is_file():raise ValueError('本机备份不存在。')
        with self.store.connect() as db:r=db.execute("SELECT digest FROM backups WHERE id=? AND state='ready'",(bid,)).fetchone()
        try:
            if any(p.is_symlink() for p in path.rglob('*')):raise ValueError()
            manifest=json.loads((path/'manifest.json').read_text());actual=tree_hash(path)
            registered=digest({'tree':actual,'manifest':manifest,'archive':hashlib.sha256((path/'snapshot.zip').read_bytes()).hexdigest()})
            if not r or manifest.get('digest')!=actual or registered!=r[0]:raise ValueError()
        except (ValueError,OSError):raise ValueError('备份校验失败，不能恢复。') from None
        return path
    def preview(self,bid):
        with self.n.serial():
            path=self.backup_path(bid);self.ensure_idle();manifest=json.loads((path/'manifest.json').read_text())
            if manifest['format']!='mem0-workbench-p7-v1' or manifest['profile']!=PROFILE:raise ValueError('备份格式或方案不匹配。')
            with self.store.connect() as db:current=db.execute("SELECT count(*) FROM controls WHERE state!='deleted'").fetchone()[0]
            return {'backup_id':bid,'current_items':current,'backup_items':manifest['items'],'expected_state':self.restore_state(),'current_generation':self.active,'derived_context_reset':True,'deletion_barriers_preserved':True}
    def restore(self,bid,expected):
        with self.n.serial(),self.n.registry.guard():
            self.n.registry.require_active(PROFILE);self.ensure_idle();path=self.backup_path(bid)
            if expected!=self.restore_state():raise ValueError('当前原生记忆已改变，请重新预览恢复。')
            manifest=json.loads((path/'manifest.json').read_text())
            if manifest['format']!='mem0-workbench-p7-v1' or manifest['profile']!=PROFILE:raise ValueError('备份格式或方案不匹配。')
            # Preserve a complete current snapshot before replacing anything.
            rollback=uuid4().hex;rollback_path=self.root/'restore-safety'/rollback
            subs=self.substores()
            for sub in subs:
                copy_tree(sub.path(),rollback_path/(sub.n.channel_kind+'-storage'));snapshot_sqlite(sub.store.path,rollback_path/(sub.n.channel_kind+'-management.sqlite'))
            copy_tree(self.path(),rollback_path/'storage');snapshot_sqlite(self.store.path,rollback_path/'management.sqlite');snapshot_sqlite(self.n.legacy.path,rollback_path/'engine.sqlite')
            gid=uuid4().hex;copy_tree(path/'storage',self.path(gid))
            with self.store.connect() as db:
                deleted={r[0] for r in db.execute('SELECT memory_id FROM tombstones')}
                current_contexts=[tuple(r) for r in db.execute('SELECT thread_id,space_id,version FROM contexts')]
            saved=sqlite3.connect(f'file:{path / "management.sqlite"}?mode=ro',uri=True)
            try:
                combined={r[0]:tuple(r) for r in saved.execute('SELECT thread_id,space_id,version FROM contexts')}
                for row in current_contexts:
                    old=combined.get(row[0]);combined[row[0]]=(row[0],row[1],max(row[2],old[2] if old else 0))
                current_contexts=list(combined.values())
            finally:saved.close()
            pin=manifest['embedding'];prefs=AppSettings(self.n.memory.settings)
            profile=prefs.profile(pin.get('profile_id') or self.n.legacy.config()['mem0'].get('embedding_profile_id'),kind='embedding')
            if signature(profile)!=pin['signature']:raise ValueError('备份使用的嵌入模型配置不可用，请先恢复对应模型配置。')
            with self.target_client(gid,profile,pin['dimensions']) as sdk:
                # Current deletion decisions survive restoration. Remove content,
                # including old native history, from the staged restored backend.
                for mid in deleted:
                    if sdk.get(mid):sdk.vector_store.delete(vector_id=mid)
                    with sdk.db._lock,sdk.db.connection:sdk.db.connection.execute('UPDATE history SET old_memory=NULL,new_memory=NULL WHERE memory_id=?',(mid,))
            pins={}
            for sub in subs:
                kind=sub.n.channel_kind;channel=manifest.get('channels',{}).get(kind)
                if not channel:
                    with sub.store.connect() as cdb:
                        if cdb.execute("SELECT 1 FROM controls WHERE state!='deleted'").fetchone():raise ValueError('此旧备份未包含当前全部记忆类型，不能执行部分覆盖。')
                    continue
                pin_sub=channel['embedding'];profile_sub=prefs.profile(pin_sub.get('profile_id'),kind='embedding')
                if signature(profile_sub)!=pin_sub['signature']:raise ValueError('备份通道的嵌入配置不可用。')
                copy_tree(path/(kind+'-storage'),sub.path(gid))
                with sub.store.connect() as cdb:deleted_sub={r[0] for r in cdb.execute('SELECT memory_id FROM tombstones')}
                with sub.target_client(gid,profile_sub,pin_sub['dimensions']) as sdk:
                    for mid in deleted_sub:
                        if sdk.get(mid):sdk.vector_store.delete(vector_id=mid)
                        with sdk.db._lock,sdk.db.connection:sdk.db.connection.execute('UPDATE history SET old_memory=NULL,new_memory=NULL WHERE memory_id=?',(mid,))
                pins[kind]=pin_sub
            self.n.fault('restore_before_publish',gid)
            with self.store.connect() as db:
                db.execute('ATTACH DATABASE ? AS saved',(str(path/'management.sqlite'),));db.execute('ATTACH DATABASE ? AS engine',(str(self.n.legacy.path),));db.execute('ATTACH DATABASE ? AS oldengine',(str(path/'engine.sqlite'),))
                for sub in subs:
                    kind=sub.n.channel_kind
                    if kind in pins:
                        db.execute('ATTACH DATABASE ? AS channel_'+kind,(str(sub.store.path),));db.execute('ATTACH DATABASE ? AS saved_'+kind,(str(path/(kind+'-management.sqlite')),))
                db.execute('BEGIN IMMEDIATE')
                # Governing exclusions only accumulate through restore. Pending
                # historical operations are not resurrected or replayed.
                for table in ('tombstones','blocked_hashes','source_barriers','context_exclusions','context_barriers'):
                    db.execute(f'INSERT OR IGNORE INTO {table} SELECT * FROM saved.{table}')
                for table in ('controls','native_sources','id_map'):
                    db.execute(f'DELETE FROM {table}');db.execute(f'INSERT INTO {table} SELECT * FROM saved.{table}')
                db.execute("UPDATE controls SET state='deleted' WHERE id IN (SELECT memory_id FROM tombstones)")
                db.execute("UPDATE id_map SET history='[]' WHERE native_id IN (SELECT memory_id FROM tombstones)")
                db.execute("UPDATE operations SET state='cancelled',reason='snapshot_restored',source='' WHERE state NOT IN ('completed','cancelled','skipped','reconciled')")
                db.execute("UPDATE interventions SET state='dismissed',proposal='' WHERE state='pending'")
                db.execute('DELETE FROM category_batches')
                db.execute('DELETE FROM contexts')
                for thread,sid,ver in current_contexts:
                    db.execute('INSERT INTO contexts VALUES (?,?,?,\'{}\',\'{}\',\'{}\',\'[]\',\'{}\',\'snapshot_restored\',?,0)',(thread,sid,ver+1,time.time()))
                    db.execute('INSERT OR REPLACE INTO context_barriers VALUES (?,?,?)',(sid,thread,time.time()))
                db.execute('INSERT OR IGNORE INTO engine.spaces SELECT * FROM oldengine.spaces')
                cfg=json.loads(db.execute('SELECT value FROM engine.config WHERE id=1').fetchone()[0]);cfg['mem0']['embedding_profile_id']=profile.id
                db.execute('UPDATE engine.config SET value=?,revision=revision+1 WHERE id=1',(json.dumps(cfg),))
                for key,val in [('active_generation',gid),('embedding',pin)]:db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,json.dumps(val)))
                db.execute("UPDATE generations SET state='retired' WHERE state='active'")
                db.execute('INSERT INTO generations VALUES (?,\'active\',?,?,?,?,?,?,\'restored\',?)',(gid,self.active,json.dumps(safe_profile(profile)),expected,manifest['items'],manifest['items'],pin['dimensions'],time.time()))
                for kind,pin_sub in pins.items():
                    owner='channel_'+kind;old='saved_'+kind
                    for table in ('tombstones','blocked_hashes','source_barriers','context_exclusions','context_barriers','method_blocks'):
                        if db.execute(f"SELECT 1 FROM {old}.sqlite_master WHERE name=?",(table,)).fetchone():db.execute(f'INSERT OR IGNORE INTO {owner}.{table} SELECT * FROM {old}.{table}')
                    for table in ('controls','native_sources','id_map','methods','method_revisions'):
                        db.execute(f'DELETE FROM {owner}.{table}')
                        if db.execute(f"SELECT 1 FROM {old}.sqlite_master WHERE name=?",(table,)).fetchone():db.execute(f'INSERT INTO {owner}.{table} SELECT * FROM {old}.{table}')
                    db.execute(f"UPDATE {owner}.controls SET state='deleted' WHERE id IN (SELECT memory_id FROM {owner}.tombstones)")
                    db.execute(f"UPDATE {owner}.methods SET state='deleted',title='',enabled=0,active_id=NULL,candidate_id=NULL,previous_id=NULL WHERE key IN (SELECT key FROM {owner}.method_blocks WHERE space_id={owner}.methods.space_id)")
                    db.execute(f"UPDATE {owner}.operations SET state='cancelled',source='',reason='snapshot_restored' WHERE state NOT IN ('completed','cancelled','skipped','reconciled')")
                    for key,value in [('active_generation',gid),('embedding',pin_sub)]:db.execute(f'INSERT OR REPLACE INTO {owner}.meta VALUES (?,?)',(key,json.dumps(value)))
                self.n.fault('restore_before_commit',gid)
            return {'restored':True,'generation':gid,'safety_snapshot':rollback,'derived_context_reset':True}
