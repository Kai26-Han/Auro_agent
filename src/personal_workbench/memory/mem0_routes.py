"""Mem0 instance-specific native management API."""
import json
from typing import Literal
from fastapi import Query
from pydantic import BaseModel,Field

class WriteInput(BaseModel):
    category:Literal["info","preference","goal","constraint","other"]|None=None
    content:str=Field(min_length=1,max_length=1000)
    version:int|None=Field(default=None,ge=1)
class StateInput(BaseModel):
    version:int=Field(ge=1)
    state:Literal['active','archived']
    locked:bool
class DeleteInput(BaseModel):
    version:int=Field(ge=1)
class SearchInput(BaseModel):
    query:str=Field(min_length=1,max_length=200)

def install(app,memory):
    native=memory.native_mem0
    from .mem0_categories import NativeCategories,CategoryEdit
    from .mem0_channels import CATEGORIES,capabilities
    categories=NativeCategories(native)
    @app.get('/api/memory/mem0/capabilities')
    def describe():return capabilities()
    @app.get('/api/memory/mem0/{sid}/groups')
    def groups(sid:str,q:str=Query(default='',max_length=200),pages:str=Query(default='{}',max_length=300)):
        try:parsed=json.loads(pages)
        except (ValueError,TypeError):raise ValueError('无效的分类页码。') from None
        if not isinstance(parsed,dict) or any(k not in CATEGORIES or type(v)!=int or not 1<=v<=100000 for k,v in parsed.items()):raise ValueError('无效的分类页码。')
        return categories.groups(sid,q,parsed)
    @app.get('/api/memory/mem0/{sid}/categories/{category}')
    def group_read(sid:str,category:str):return categories.read(sid,category)
    @app.post('/api/memory/mem0/{sid}/categories/{category}/preview')
    def group_preview(sid:str,category:str,body:CategoryEdit):return categories.preview(sid,category,body)
    @app.put('/api/memory/mem0/{sid}/categories/{category}')
    def group_save(sid:str,category:str,body:CategoryEdit):
        with app.state.jobs.lock:
            if app.state.jobs.active:raise ValueError('请等待当前对话完成后再保存记忆。')
            return categories.save(sid,category,body)
    @app.post('/api/memory/mem0/{sid}/category-batches/{bid}/retry')
    def group_retry(sid:str,bid:str):
        with app.state.jobs.lock:
            if app.state.jobs.active:raise ValueError('请等待当前对话完成后再保存记忆。')
            return categories.retry(sid,bid)
    @app.get('/api/memory/mem0/{sid}/dashboard')
    def dashboard(sid:str,page:int=Query(default=1,ge=1,le=100000)):
        return native.dashboard(sid,page)
    @app.get('/api/memory/mem0/{sid}/items')
    def items(sid:str,q:str=Query(default='',max_length=200),status:Literal['active','archived','all']='active',page:int=Query(default=1,ge=1,le=100000)):
        return native.listing(sid,q,status,page)
    @app.get('/api/memory/mem0/{sid}/items/{mid}')
    def detail(sid:str,mid:str):return native.detail(sid,mid)
    @app.post('/api/memory/mem0/{sid}/items')
    def create(sid:str,body:WriteInput):return native.new_write(sid,'manual_add',body.content,category=body.category)
    @app.put('/api/memory/mem0/{sid}/items/{mid}')
    def edit(sid:str,mid:str,body:WriteInput):return native.new_write(sid,'edit',body.content,mid,body.version,category=body.category)
    @app.put('/api/memory/mem0/{sid}/items/{mid}/state')
    def state(sid:str,mid:str,body:StateInput):return native.manage_state(sid,mid,body.version,body.state,body.locked)
    @app.delete('/api/memory/mem0/{sid}/items/{mid}')
    def delete(sid:str,mid:str,body:DeleteInput):return native.new_write(sid,'delete',mid=mid,version=body.version)
    @app.post('/api/memory/mem0/{sid}/search')
    def search(sid:str,body:SearchInput):return memory.preview(sid,body.query)
    @app.post('/api/memory/mem0/{sid}/jobs/{oid}/{action}')
    def action(sid:str,oid:str,action:Literal['reconcile','retry','cancel']):
        if native.store.operation(oid)['space_id']!=sid:raise ValueError('操作不属于此空间。')
        return native.reconcile(oid) if action=='reconcile' else native.job_action(oid,action)
    @app.post('/api/memory/mem0/{sid}/interventions/{iid}/dismiss')
    def dismiss(sid:str,iid:str):
        native.require_space(sid)
        with native.store.connect() as db:
            if not db.execute('SELECT 1 FROM interventions WHERE id=? AND space_id=?',(iid,sid)).fetchone():raise ValueError('待处理建议不存在。')
        return native.dismiss(iid)
    @app.post('/api/memory/mem0/migrate')
    def migrate():return native.migrate()

    from .mem0_events import Events,EventEdit
    def event_service():return Events(native)
    @app.get('/api/memory/mem0/{sid}/events')
    def event_list(sid:str,q:str=Query(default='',max_length=200),topic:str=Query(default='',max_length=80),page:int=Query(default=1,ge=1,le=100000)):
        return event_service().listing(sid,q,topic,page)
    def event_idle():
        if app.state.jobs.active:raise ValueError('请等待当前对话完成后再保存记忆。')
    @app.post('/api/memory/mem0/{sid}/events')
    def event_create(sid:str,body:EventEdit):
        with app.state.jobs.lock:
            event_idle();return event_service().write(sid,body)
    @app.put('/api/memory/mem0/{sid}/events/{mid}')
    def event_edit(sid:str,mid:str,body:EventEdit):
        with app.state.jobs.lock:
            event_idle();return event_service().write(sid,body,mid)
    @app.delete('/api/memory/mem0/{sid}/events/{mid}')
    def event_delete(sid:str,mid:str,body:DeleteInput):
        with app.state.jobs.lock:
            event_idle();return event_service().n.new_write(sid,'delete',mid=mid,version=body.version)
    @app.post('/api/memory/mem0/{sid}/event-operations/{oid}/resolve')
    def event_resolve(sid:str,oid:str):
        with app.state.jobs.lock:
            event_idle();return event_service().retry(sid,oid)

    from .mem0_procedures import Procedures,MethodCreate,MethodEdit,MethodAction
    @app.get('/api/memory/mem0/{sid}/methods')
    def methods(sid:str,q:str=Query(default='',max_length=200),page:int=Query(default=1,ge=1,le=100000)):
        return Procedures(native).listing(sid,q,page)
    @app.post('/api/memory/mem0/{sid}/methods')
    def method_create(sid:str,body:MethodCreate):
        with app.state.jobs.lock:
            event_idle();return Procedures(native).create(sid,body)
    @app.put('/api/memory/mem0/{sid}/methods/{mid}')
    def method_edit(sid:str,mid:str,body:MethodEdit):
        with app.state.jobs.lock:
            event_idle();return Procedures(native).edit(sid,mid,body)
    @app.post('/api/memory/mem0/{sid}/methods/{mid}/{action}')
    def method_action(sid:str,mid:str,action:Literal['enable','disable','rollback','delete','discard','lock','unlock'],body:MethodAction):
        with app.state.jobs.lock:
            event_idle();return Procedures(native).act(sid,mid,body.version,action)
    @app.post('/api/memory/mem0/{sid}/procedure-operations/{oid}/resolve')
    def procedure_resolve(sid:str,oid:str):
        with app.state.jobs.lock:
            event_idle();return Procedures(native).resolve(sid,oid)
