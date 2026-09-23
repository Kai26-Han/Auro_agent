"""Whole-category editing with a durable, recoverable native write plan.

A pending batch freezes the space (reads and competing writes). Each step uses
its own SDK instance and deterministic operation identity. Recovery inspects
vector/history evidence before repeating a provably unapplied operation.
"""
import hashlib
import json
import re
import time
from collections import defaultdict, deque
from uuid import uuid4
from pydantic import BaseModel, Field
from .mem0_channels import CATEGORIES, category
from .mem0_native_bridge import fingerprint


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def line(text):
    return re.sub(r'\s*\n\s*', ' ', text).strip()


class CategoryEdit(BaseModel):
    token: str = Field(min_length=64, max_length=64)
    content: str = Field(max_length=200000)
    confirmation: str | None = Field(default=None, min_length=64, max_length=64)
    locked: bool | None = None


class NativeCategories:
    def __init__(self, native):
        self.n = native
        self.store = native.store

    def rows(self, sdk, sid, group=None):
        self.n.require_space(sid)
        if group is not None: category(group)
        rows = self.n.visible(sdk, sid)
        for r in rows:
            if r['category'] not in CATEGORIES: r['category'] = 'other'
        return sorted((r for r in rows if group is None or r['category'] == group),
                      key=lambda r: (r['created'], r['id']))

    def groups(self, sid, query='', pages=None):
        self.n.require_space(sid)
        pending = self.store.batch(sid)
        if pending: return {'ready': self.n.ready, 'pending': pending, 'groups': []}
        if not self.n.ready: return {'ready': False, 'pending': None, 'groups': []}
        with self.n.serial(), self.n.client() as sdk:
            # Recheck under the same serialization lock as writes.
            pending = self.store.batch(sid)
            if pending: return {'ready': True, 'pending': pending, 'groups': []}
            rows = self.rows(sdk, sid)
        result = []
        for key, label in CATEGORIES.items():
            selected = [r for r in rows if r['category'] == key and query.casefold() in r['memory'].casefold()]
            page = max(1, min((pages or {}).get(key, 1), max(1, (len(selected)+19)//20)))
            result.append({'key': key, 'label': label, 'page': page, 'more': page*20 < len(selected),
                           'items': [{'id': r['id'], 'content': r['memory']} for r in selected[(page-1)*20:page*20]]})
        return {'ready': True, 'pending': None, 'groups': result}

    def snapshot(self, rows):
        return digest([(r['id'],r['version'],r['state'],r['locked'],r['memory'],r['category']) for r in rows])

    def read(self, sid, group):
        with self.n.serial(), self.n.client() as sdk:
            rows = self.rows(sdk, sid, group)
            content = '\n'.join(line(r['memory']) for r in rows)
            if len(content) > 200000: raise ValueError('此类型内容超过统一编辑上限，请先通过原生管理接口分批整理。')
            return {'token': self.snapshot(rows), 'content': content}

    def plan(self, sdk, sid, group, body):
        rows = self.rows(sdk, sid, group)
        if body.token != self.snapshot(rows): raise ValueError('此类型的记忆已更新，请重新打开管理后再保存。')
        values = [v.strip() for v in body.content.splitlines() if v.strip()]
        available = defaultdict(deque)
        for r in rows: available[line(r['memory'])].append(r)
        matched, new = set(), []
        for value in values:
            if available[value]: matched.add(available[value].popleft()['id'])
            else:
                if len(value)>1000: raise ValueError('每行新建或修改的记忆最多 1000 个字符。')
                new.append(value)
        remaining = [r for r in rows if r['id'] not in matched]
        edits = list(zip(remaining, new))
        removed, inserted = remaining[len(new):], new[len(remaining):]
        steps = [{'kind':'edit','mid':r['id'],'content':v} for r,v in edits]
        steps += [{'kind':'delete','mid':r['id'],'content':''} for r in removed]
        steps += [{'kind':'manual_add','mid':None,'content':v} for v in inserted]
        if body.locked is not None:
            # New records and edits are protected by default. Applying a requested
            # group policy happens after all native writes, before publication.
            steps += [{'kind':'protection','mid':r['id'],'locked':body.locked} for r in rows if r not in removed]
            steps += [{'kind':'protection','from_step':i,'locked':body.locked} for i,s in enumerate(steps.copy()) if s['kind']=='manual_add']
        with self.store.connect() as db:
            for v in new:
                if db.execute('SELECT 1 FROM blocked_hashes WHERE space_id=? AND hash=?',(sid,fingerprint(v))).fetchone():
                    raise ValueError('内容与已删除的记忆重复，请调整后再保存。')
            # Deletion invalidates source-related short-term contexts. Bind this
            # impact to the confirmation so a changed context needs new consent.
            contexts = [tuple(r) for r in db.execute('SELECT thread_id,version FROM contexts WHERE space_id=? ORDER BY thread_id',(sid,))] if removed else []
        confirmation = digest([sid,group,body.token,body.content,body.locked,contexts])
        return steps, {'confirmation':confirmation,'removed':[r['memory'] for r in removed],
                       'context_reset':bool(removed)}

    def preview(self, sid, group, body):
        with self.n.serial(), self.n.client() as sdk:
            return self.plan(sdk, sid, group, body)[1]

    def save(self, sid, group, body):
        self.n.require_space(sid);category(group)
        request_hash = digest([sid,group,body.model_dump()])
        with self.n.serial():
            self.n.frozen(sid)
            with self.store.connect() as db:
                prior = db.execute('SELECT id,state FROM category_batches WHERE space_id=? AND request_hash=? ORDER BY created DESC LIMIT 1',(sid,request_hash)).fetchone()
            if prior:
                return {'saved':prior['state']=='completed','status':prior['state'],'batch_id':prior['id']}
            self.store.require_no_batch(sid)
            if not self.n.ready: raise ValueError('此空间的记忆存储尚未就绪。')
            with self.n.client() as sdk:
                steps, preview = self.plan(sdk,sid,group,body)
            if preview['removed'] and body.confirmation != preview['confirmation']:
                raise ValueError('请先确认将删除的记忆；内容或关联上下文变化后需重新确认。')
            bid, stamp = uuid4().hex, time.time()
            frozen = self.n.frozen(sid)
            with self.n.registry.guard(),self.store.connect() as db:
                self.n.check_frozen(frozen)
                db.execute('INSERT INTO category_batches VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (bid,sid,group,'pending',request_hash,json.dumps(steps,ensure_ascii=False),0,json.dumps(frozen),stamp,stamp))
            return self.run(bid)

    def retry(self, sid, bid):
        self.n.require_space(sid)
        with self.n.serial():
            self.n.frozen(sid)
            with self.store.connect() as db:
                row = db.execute('SELECT space_id FROM category_batches WHERE id=?',(bid,)).fetchone()
            if not row or row['space_id'] != sid: raise ValueError('分类保存记录不属于此空间。')
            return self.run(bid)

    def run(self, bid):
        with self.store.connect() as db:
            batch = dict(db.execute('SELECT * FROM category_batches WHERE id=?',(bid,)).fetchone())
        if batch['state']=='completed': return {'saved':True,'status':'completed','batch_id':bid}
        sid, steps = batch['space_id'], json.loads(batch['plan'])
        try:
            execution = self.n.frozen(sid)
            for index in range(batch['cursor'],len(steps)):
                step = steps[index]
                frozen = {**execution,'_batch':bid,'_category':batch['category'],'_target':step.get('mid')}
                if step['kind']=='protection':
                    self.protect(batch,index,step,steps,frozen)
                    continue
                oid = self.store.start(sid,step['kind'],step['content'],frozen,run=f'batch:{bid}:{index}')
                with self.n.client() as sdk:
                    op = self.store.operation(oid)
                    with self.store.connect() as db:
                        actions = db.execute('SELECT * FROM actions WHERE operation_id=?',(oid,)).fetchall()
                    for action in actions:
                        if action['state']=='prepared': self.n.verify_action(sdk,action['id'],repair=True)
                    with self.store.connect() as db:
                        applied = db.execute("SELECT 1 FROM actions WHERE operation_id=? AND state='verified'",(oid,)).fetchone()
                    if not applied:
                        # No native side effect, or repair proved the prior attempt
                        # did not happen. Only then is deterministic replay allowed.
                        with self.store.connect() as db:
                            db.execute("UPDATE operations SET state='running',source=?,frozen=? WHERE id=?",(step['content'],json.dumps(frozen),oid))
                        if step['kind']=='delete' and self.store.control(step['mid'])['state']!='deleted':
                            self.n.barrier(sdk,sid,step['mid'])
                        result = self.n.execute(sdk,self.store.operation(oid),step.get('mid'))
                        if result['status']!='completed' or result['count']!=1: raise ValueError('Native category step incomplete')
                    if step['kind']=='delete': self.n.scrub(sdk,step['mid'])
                    self.store.finish(oid,'completed')
                self.n.fault('category_before_advance',bid)
                with self.n.registry.guard(),self.store.connect() as db:
                    self.n.check_frozen(frozen)
                    db.execute('UPDATE category_batches SET cursor=?,updated=? WHERE id=?',(index+1,time.time(),bid))
            with self.n.registry.guard(),self.store.connect() as db:
                self.n.check_frozen(execution)
                db.execute("UPDATE category_batches SET state='completed',plan='[]',updated=? WHERE id=?",(time.time(),bid))
            return {'saved':True,'status':'completed','batch_id':bid}
        except Exception:
            # Keep the durable plan; no content or transport secrets in the error.
            return {'saved':False,'status':'pending','batch_id':bid}

    def protect(self,batch,index,step,steps,frozen):
        mid=step.get('mid')
        if not mid:
            run=f"batch:{batch['id']}:{step['from_step']}"
            with self.store.connect() as db:
                row=db.execute("SELECT a.memory_id FROM actions a JOIN operations o ON o.id=a.operation_id WHERE o.space_id=? AND o.run_id=? AND a.state='verified'",(batch['space_id'],run)).fetchone()
            if not row: raise ValueError('新记忆尚未写入。')
            mid=row[0]
        with self.n.registry.guard(),self.store.connect() as db:
            self.n.check_frozen(frozen)
            row=db.execute('SELECT * FROM controls WHERE id=? AND space_id=?',(mid,batch['space_id'])).fetchone()
            if not row or row['state']=='deleted': raise ValueError('记忆已改变。')
            if bool(row['locked']) != step['locked']:
                db.execute('UPDATE controls SET locked=?,version=version+1,updated=? WHERE id=?',(int(step['locked']),time.time(),mid))
            db.execute('UPDATE category_batches SET cursor=?,updated=? WHERE id=?',(index+1,time.time(),batch['id']))
