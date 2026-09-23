"""Separate context and storage management routes; no LangMem APIs are reused."""
import json
from contextlib import contextmanager
from dataclasses import replace
from fastapi.responses import FileResponse
from fastapi import Query
from pydantic import BaseModel,Field
from typing import Literal
from personal_workbench.assistant_service import open_service,LazyModel
from personal_workbench.app_settings import AppSettings
from .mem0_context import Contexts,Mem0Context,TaskEdit,ContextVersion,Exclusion
from .mem0_storage import Storage
class GenerationInput(BaseModel):
    embedding_profile_id:str=Field(pattern=r'^[a-zA-Z0-9_-]{1,64}$')
class RestoreInput(BaseModel):
    expected_state:str=Field(pattern=r'^[a-f0-9]{64}$')

def install(app,memory):
    native=memory.native_mem0;contexts=Contexts(native);storage=Storage(native);jobs=app.state.jobs
    @contextmanager
    def idle():
        with jobs.lock:
            if jobs.active:raise ValueError('请等待当前对话完成后再管理 Mem0 上下文或存储。')
            with open_service(memory.settings) as service:yield service
    def bound(service,sid,tid):
        snapshot=json.loads(service.session(tid)['skill_snapshot'] or '{}');binding=snapshot.get('memory') or {}
        if binding.get('memory_profile_id')!='mem0-default' or binding.get('space_id')!=sid:raise ValueError('会话未绑定此 Mem0 空间。')
        if not binding.get('enabled',True):raise ValueError('此会话未启用记忆方案。')
        return binding
    @app.get('/api/memory/mem0/{sid}/contexts')
    def context_list(sid:str,offset:int=Query(default=0,ge=0)):return contexts.listing(sid,offset)
    @app.get('/api/memory/mem0/{sid}/contexts/{tid}')
    def context_detail(sid:str,tid:str):
        with open_service(memory.settings,read_only=True) as service:bound(service,sid,tid)
        return contexts.public(sid,tid)
    @app.put('/api/memory/mem0/{sid}/contexts/{tid}/task')
    def task(sid:str,tid:str,body:TaskEdit):
        with idle() as service:
            bound(service,sid,tid)
            if service.status(tid)['next']:raise ValueError('请先完成或恢复当前任务，再修改上下文。')
            return contexts.edit_task(native.frozen(sid),tid,body)
    @app.put('/api/memory/mem0/{sid}/contexts/{tid}/source')
    def source(sid:str,tid:str,body:Exclusion):
        with idle() as service:
            bound(service,sid,tid);current=service.status(tid)
            if current['next']:raise ValueError('请先完成或恢复当前任务，再调整来源。')
            return contexts.exclude(native.frozen(sid),tid,body,current['state'].get('messages',[]))
    @app.post('/api/memory/mem0/{sid}/contexts/{tid}/rebuild')
    def rebuild(sid:str,tid:str,body:ContextVersion):
        with idle() as service:
            bound(service,sid,tid);current=service.status(tid)
            if current['next']:raise ValueError('请先完成或恢复当前任务，再重建上下文。')
            if contexts.get(sid,tid)['version']!=body.version:raise ValueError('Mem0 上下文已更新，请刷新。')
            runtime=AppSettings(memory.settings).runtime(current.get('model_profile_id'))
            model=LazyModel(replace(runtime,max_tokens=min(768,runtime.context_window//16),timeout=min(runtime.timeout,45)),[])
            frozen=native.frozen(sid)
            if frozen['mem0'].get('context_strategy','recent')!='summary':raise ValueError('请先在 Mem0 引擎中选择摘要衔接模式。')
            policy=Mem0Context(runtime,native,frozen,tid,model)
            policy.prepare(current['state'],[],[],force=True)
            return contexts.public(sid,tid)
    @app.get('/api/memory/mem0/storage/status')
    def diagnostics():return storage.diagnostics()
    @app.post('/api/memory/mem0/storage/check')
    def check():return storage.check()
    @app.post('/api/memory/mem0/storage/generations')
    def start(body:GenerationInput):
        with idle():return storage.start(body.embedding_profile_id)
    @app.post('/api/memory/mem0/storage/generations/{gid}/{action}')
    def generation_action(gid:str,action:Literal['continue','publish','cancel']):
        with idle():return {'continue':storage.step,'publish':storage.publish,'cancel':storage.cancel}[action](gid)
    @app.post('/api/memory/mem0/storage/backups')
    def backup():
        with idle():return storage.backup()
    @app.get('/api/memory/mem0/storage/backups/{bid}/download')
    def download(bid:str):
        return FileResponse(storage.backup_path(bid)/'snapshot.zip',media_type='application/zip',filename='mem0-backup-'+bid[:8]+'.zip',headers={'Cache-Control':'no-store'})
    @app.post('/api/memory/mem0/storage/backups/{bid}/preview')
    def preview(bid:str):
        with idle():return storage.preview(bid)
    @app.post('/api/memory/mem0/storage/backups/{bid}/restore')
    def restore(bid:str,body:RestoreInput):
        with idle():return storage.restore(bid,body.expected_state)
