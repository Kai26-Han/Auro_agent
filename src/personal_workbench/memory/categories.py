"""Edit an entire fact category with optimistic concurrency and one transaction."""
import hashlib
import json
import re
from collections import defaultdict, deque
from pydantic import BaseModel, Field
from .store import fingerprint, now

CATEGORIES={'fact','preference','goal','experience'}

def line(text):
    return re.sub(r'\s*\n\s*',' ',text).strip()

class CategoryEdit(BaseModel):
    token: str = Field(min_length=64,max_length=64)
    content: str = Field(max_length=200000)
    confirmation: str | None = Field(default=None,min_length=64,max_length=64)

class FactCategories:
    def __init__(self,memory):
        self.memory=memory
        self.store=memory.store.stores['langmem']
        self.registry=memory.store.registry

    def rows(self,db,sid,category):
        self.memory.store.require_langmem(sid)
        if category not in CATEGORIES:raise ValueError('记忆类型无效。')
        return [dict(r) for r in db.execute("SELECT * FROM memories WHERE space_id=? AND category=? AND memory_type='fact' AND deleted=0 ORDER BY created,rowid",(sid,category))]

    def snapshot(self,rows):
        return hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

    def read(self,sid,category):
        with self.store.connect() as db:rows=self.rows(db,sid,category)
        return {'token':self.snapshot(rows),'content':'\n'.join(line(r['content']) for r in rows)}

    def plan(self,db,sid,category,body):
        values=[s.strip() for s in body.content.splitlines() if s.strip()]
        if any(len(s)>1000 for s in values):raise ValueError('每行记忆最多 1000 个字符。')
        rows=self.rows(db,sid,category)
        if self.snapshot(rows)!=body.token:raise ValueError('此类型的记忆已更新，请重新打开管理后再保存。')
        available=defaultdict(deque)
        for row in rows:available[line(row['content'])].append(row)
        matched=set();new=[]
        for value in values:
            if available[value]:matched.add(available[value].popleft()['id'])
            else:new.append(value)
        remaining=[r for r in rows if r['id'] not in matched]
        updates=list(zip(remaining,new));removed=remaining[len(new):];inserts=new[len(remaining):]
        deletion=[{'id':r['id'],'version':r['version'],'content':r['content']} for r in removed]
        confirmation=hashlib.sha256(json.dumps([body.token,body.content,deletion],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        return updates,removed,inserts,confirmation

    def preview(self,sid,category,body):
        # Lifecycle tables must exist before opening the snapshot transaction.
        self.memory.lifecycle
        with self.registry.guard(),self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _,removed,_,confirmation=self.plan(db,sid,category,body)
        return {'confirmation':confirmation,'removed':[r['content'] for r in removed],'related':[]}

    def save(self,sid,category,body):
        lifecycle=self.memory.lifecycle
        with self.registry.guard(),self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            updates,removed,inserts,confirmation=self.plan(db,sid,category,body)
            if removed and body.confirmation!=confirmation:
                raise ValueError('请先确认将删除的记忆；内容或记忆列表变化后需重新确认。')
            deleted={r['id'] for r in removed}
            if removed:lifecycle.delete_memories_in(db,sid,deleted,'category:'+category)
            for before,content in updates:
                if before['id'] in deleted:continue
                db.execute("UPDATE memories SET content=?,status='active',locked=1,manual=1,source_kind='manual',version=version+1,updated=?,fingerprint=? WHERE id=?",(content,now(),fingerprint(content),before['id']))
                row=dict(db.execute('SELECT * FROM memories WHERE id=?',(before['id'],)).fetchone())
                self.store._change(db,row,'MANUAL_UPDATE',before=before)
                db.execute('DELETE FROM memory_vectors WHERE memory_id=?',(before['id'],))
                db.execute("UPDATE memory_reviews SET status='stale',resolved=? WHERE target_id=? AND status='pending'",(now(),before['id']))
            for value in inserts:self.store._insert(db,sid,value,category,manual=True,metadata={'locked':True})
            if updates or inserts:self.store.touch_epoch(db,sid)
        return {'saved':True}
