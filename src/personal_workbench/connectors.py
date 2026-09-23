"""S3: explicit MCP connections, frozen tool grants and durable mutation receipts.

Each connection owns a thread and a single async task. Transport, session and
cancel scopes enter/exit in that task; synchronous LangGraph nodes use a queue.
No server-supplied prompts, sampling requests or resources are auto-executed.
"""
from personal_workbench.connector_limits import MAX_CONNECTOR_TOOLS_PER_TURN
import asyncio
import base64
import concurrent.futures
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from contextlib import AsyncExitStack, contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import httpx2
import httpx
from jsonschema import Draft202012Validator
from langchain_core.tools import StructuredTool
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field, model_validator


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def default_tool_policy(read_hint):
    """Enable discovered capabilities while keeping unverified writes approval-gated."""
    return 'read' if read_hint else 'confirm'


class ConnectorInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80)
    transport: Literal['http', 'stdio'] = 'http'
    url: str = Field(default='', max_length=2000)
    command: str = Field(default='', max_length=2000)
    args: list[str] = Field(default_factory=list, max_length=40)
    timeout: int = Field(default=30, ge=2, le=120)
    token: str | None = Field(default=None, max_length=8000)
    env: dict[str, str] | None = None
    clear_token: bool = False
    clear_env: bool = False
    package_id: str | None = Field(default=None, pattern=r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
    package_version: str | None = Field(default=None, max_length=80)
    package_digest: str | None = Field(default=None,pattern=r'^[a-f0-9]{64}$')
    package_description: str = Field(default='', max_length=1000)
    oauth: dict | None = None
    dependencies: dict = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_config(self):
        if not self.name.strip(): raise ValueError('请输入连接器名称。')
        if self.transport == 'http':
            u = urlsplit(self.url)
            if u.scheme not in {'http', 'https'} or not u.hostname or u.username or u.password or u.query or u.fragment:
                raise ValueError('服务地址须为 HTTP(S) 地址，认证请填写 Token，不要放在地址中。')
            if u.scheme == 'http' and u.hostname not in {'localhost', '127.0.0.1', '::1'}:
                raise ValueError('远程连接请使用 HTTPS；本机服务可使用 HTTP。')
        else:
            packaged=bool(self.package_id and re.fullmatch(r'[A-Za-z0-9._+-]{1,120}',self.command))
            if not packaged and (not Path(self.command).is_absolute() or not Path(self.command).is_file() or not os.access(self.command, os.X_OK)):
                raise ValueError('请选择已安装的可执行文件绝对路径。')
        if any(len(x) > 4000 or '\0' in x for x in self.args): raise ValueError('启动参数无效。')
        if self.env and (len(self.env) > 30 or any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', k) or len(v)>8000 or '\0' in v for k,v in self.env.items())):
            raise ValueError('环境变量无效。')
        if self.token and any(c in self.token for c in '\r\n\0'): raise ValueError('Token 格式无效。')
        if self.oauth:
            required={'authorization_url','token_url','client_id','scopes','extra_authorize_params'}
            if self.transport!='http' or set(self.oauth)!=required: raise ValueError('OAuth 配置无效。')
            for key in ('authorization_url','token_url'):
                u=urlsplit(self.oauth[key])
                if u.scheme!='https' or not u.hostname or u.username or u.password or u.fragment: raise ValueError('OAuth 地址必须使用 HTTPS。')
            if not isinstance(self.oauth['client_id'],str) or not isinstance(self.oauth['scopes'],list) or not isinstance(self.oauth['extra_authorize_params'],dict): raise ValueError('OAuth 配置无效。')
        deps={**{'skills':[],'executables':[],'environment':[]},**self.dependencies}
        if set(deps)-{'skills','executables','environment'}: raise ValueError('连接器依赖声明无效。')
        self.dependencies=deps
        return self


class ToolPolicies(BaseModel):
    model_config = ConfigDict(extra='forbid')
    policies: dict[str, Literal['disabled', 'read', 'confirm']] = Field(default_factory=dict)
    idempotency_parameters: dict[str, str | None] = Field(default_factory=dict)


class ConnectorStore:
    def __init__(self, settings):
        self.path = settings.data_dir / 'connectors.sqlite'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS connectors(id TEXT PRIMARY KEY, config TEXT, revision TEXT, tools TEXT);
                CREATE TABLE IF NOT EXISTS credentials(id TEXT PRIMARY KEY, secret TEXT);
                CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY, fingerprint TEXT, status TEXT, result TEXT);
                CREATE TABLE IF NOT EXISTS oauth_states(state TEXT PRIMARY KEY, connector_id TEXT, verifier TEXT, redirect_uri TEXT, expires REAL);
                CREATE TABLE IF NOT EXISTS package_previews(id TEXT PRIMARY KEY, manifest TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS connector_migrations(connector_id TEXT PRIMARY KEY, default_policy_version INTEGER);
            ''')
        self.path.chmod(0o600)
        self._migrate_default_policies()

    def _migrate_default_policies(self):
        """One-time upgrade from the former disabled-by-default connector policy."""
        with self.db() as db:
            rows=db.execute('''SELECT id,tools FROM connectors WHERE id NOT IN
                (SELECT connector_id FROM connector_migrations WHERE default_policy_version>=1)''').fetchall()
            for row in rows:
                tools=json.loads(row['tools'])
                for item in tools:
                    if item.get('policy')=='disabled':
                        item['policy']=default_tool_policy(bool(item.get('read_hint')))
                db.execute('UPDATE connectors SET tools=? WHERE id=?',(json.dumps(tools),row['id']))
                db.execute('INSERT OR REPLACE INTO connector_migrations VALUES (?,1)',(row['id'],))

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path, timeout=10); db.row_factory=sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def raw(self, cid, summary=False):
        with self.db() as db:
            tool_column = "(SELECT json_group_array(json_remove(value, '$.schema')) FROM json_each(connectors.tools)) AS tools" if summary else 'tools'
            row=db.execute(f'SELECT id, config, revision, {tool_column} FROM connectors WHERE id=?',(cid,)).fetchone()
            secret=db.execute('SELECT secret FROM credentials WHERE id=?',(cid,)).fetchone()
        if row is None: raise ValueError('连接器不存在。')
        return {**json.loads(row['config']), 'id':cid, 'revision':row['revision'], 'tools':json.loads(row['tools']),
                '_secret':json.loads(secret['secret']) if secret else {}}

    def redact_value(self,cid,value):
        with self.db() as db:
            row=db.execute('SELECT secret FROM credentials WHERE id=?',(cid,)).fetchone()
        secret=json.loads(row['secret']) if row else {}
        values=[v for v in [secret.get('token'),secret.get('refresh_token'),*secret.get('env',{}).values()] if v]
        def clean(item):
            if isinstance(item,str):
                for value in values: item=item.replace(value,'[redacted]')
                return item
            if isinstance(item,list): return [clean(v) for v in item]
            if isinstance(item,dict): return {clean(k):clean(v) for k,v in item.items()}
            return item
        return clean(value)

    def public(self, cid, summary=False):
        result=self.raw(cid, summary=summary); secret=result.pop('_secret')
        oauth=bool(result.get('oauth'))
        return self.redact_value(cid, {**result, 'has_token':bool(secret.get('token')),
            'oauth_authorized':bool(oauth and secret.get('token')), 'oauth_expires_at':secret.get('expires_at') if oauth else None,
            'oauth_scopes':secret.get('scope',[]) if oauth else [], 'env_keys':sorted(secret.get('env',{}))})

    def list(self, summary=False):
        with self.db() as db: ids=[r[0] for r in db.execute('SELECT id FROM connectors ORDER BY rowid DESC')]
        return [self.public(cid, summary=summary) for cid in ids]

    def save(self, body, cid=None):
        old=self.raw(cid) if cid else None
        cid=cid or uuid4().hex[:16]
        config=body.model_dump(exclude={'token','env','clear_token','clear_env'})
        config['name']=config['name'].strip()
        if old and body.package_id is None and old.get('package_id'):
            for key in ('package_id','package_version','package_digest','package_description','oauth','dependencies'):
                config[key]=old.get(key)
        # Never retain inactive transport arguments in public configuration.
        if body.transport=='http': config.update(command='',args=[])
        else: config['url']=''
        secret=dict(old['_secret']) if old else {}
        if body.clear_token or body.transport!='http': secret.pop('token',None)
        elif body.token is not None: secret['token']=body.token
        if body.clear_env or body.transport!='stdio': secret.pop('env',None)
        elif body.env is not None: secret['env']=body.env
        # A changed endpoint/credential invalidates all previously discovered grants.
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO connectors VALUES (?,?,?,?)',(cid,json.dumps(config),uuid4().hex,'[]'))
            db.execute('INSERT OR REPLACE INTO credentials VALUES (?,?)',(cid,json.dumps(secret)))
            db.execute('INSERT OR REPLACE INTO connector_migrations VALUES (?,1)',(cid,))
        return self.public(cid)

    def preview_package(self,filename,data):
        from personal_workbench.connector_package import parse_connector_package
        manifest=parse_connector_package(filename,data); token=uuid4().hex
        with self.db() as db:
            db.execute('DELETE FROM package_previews WHERE created<?',(time.time()-3600,))
            db.execute('INSERT INTO package_previews VALUES (?,?,?)',(token,json.dumps(manifest,ensure_ascii=False),time.time()))
        return {**manifest,'preview_id':token}

    def install_package(self,token):
        if not re.fullmatch(r'[a-f0-9]{32}',token): raise ValueError('连接器安装预览无效。')
        with self.db() as db:
            row=db.execute('SELECT * FROM package_previews WHERE id=?',(token,)).fetchone()
        if not row or row['created']<time.time()-3600: raise ValueError('连接器安装预览已过期。')
        manifest=json.loads(row['manifest']); transport=manifest['transport']
        if transport=='stdio':
            command=shutil.which(manifest['stdio']['command']) or manifest['stdio']['command']
            config={'command':command,'args':manifest['stdio'].get('args',[]),'url':''}
        else: config={'url':manifest['http']['url'],'command':'','args':[]}
        body=ConnectorInput(name=manifest['name'],transport=transport,timeout=30,package_id=manifest['id'],
            package_version=manifest['version'],package_digest=manifest['package_digest'],package_description=manifest['description'],oauth=manifest.get('oauth'),
            dependencies=manifest['dependencies'],**config)
        result=self.save(body)
        with self.db() as db: db.execute('DELETE FROM package_previews WHERE id=?',(token,))
        return result

    def oauth_start(self,cid,redirect_uri):
        row=self.raw(cid); oauth=row.get('oauth')
        if not oauth: raise ValueError('此连接器没有声明 OAuth。')
        state=secrets.token_urlsafe(32); verifier=secrets.token_urlsafe(64)
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        with self.db() as db:
            db.execute('DELETE FROM oauth_states WHERE expires<?',(time.time(),))
            db.execute('INSERT INTO oauth_states VALUES (?,?,?,?,?)',(state,cid,verifier,redirect_uri,time.time()+600))
        params={**oauth.get('extra_authorize_params',{}),'response_type':'code','client_id':oauth['client_id'],'redirect_uri':redirect_uri,
                'scope':' '.join(oauth.get('scopes',[])),'state':state,'code_challenge':challenge,'code_challenge_method':'S256'}
        return {'authorization_url':oauth['authorization_url']+('?' if '?' not in oauth['authorization_url'] else '&')+urlencode(params),'expires_in':600}

    def oauth_complete(self,state,code):
        with self.db() as db:
            row=db.execute('SELECT * FROM oauth_states WHERE state=?',(state,)).fetchone()
            if row: db.execute('DELETE FROM oauth_states WHERE state=?',(state,))
        if not row or row['expires']<time.time(): raise ValueError('OAuth 授权已过期，请重新发起。')
        connector=self.raw(row['connector_id']); oauth=connector.get('oauth') or {}
        try:
            response=httpx.post(oauth['token_url'],data={'grant_type':'authorization_code','code':code,
                'redirect_uri':row['redirect_uri'],'client_id':oauth['client_id'],'code_verifier':row['verifier']},timeout=30,follow_redirects=False)
            response.raise_for_status(); token=response.json()
        except Exception: raise ValueError('OAuth Token 交换失败，请重新授权。') from None
        if not isinstance(token,dict) or not isinstance(token.get('access_token'),str): raise ValueError('OAuth 服务没有返回有效 access_token。')
        secret=connector['_secret']; secret.update(token=token['access_token'],refresh_token=token.get('refresh_token'),
            expires_at=time.time()+int(token.get('expires_in',3600)),scope=(token.get('scope') or ' '.join(oauth.get('scopes',[]))).split())
        with self.db() as db: db.execute('INSERT OR REPLACE INTO credentials VALUES (?,?)',(connector['id'],json.dumps(secret)))
        return connector['id']

    def refresh_oauth(self,cid):
        row=self.raw(cid); oauth=row.get('oauth'); secret=row['_secret']
        if not oauth or not secret.get('refresh_token') or secret.get('expires_at',0)>time.time()+60:return
        try:
            response=httpx.post(oauth['token_url'],data={'grant_type':'refresh_token','refresh_token':secret['refresh_token'],'client_id':oauth['client_id']},timeout=30,follow_redirects=False)
            response.raise_for_status(); token=response.json()
        except Exception: raise ValueError('OAuth 凭据已过期且刷新失败，请重新授权。') from None
        secret.update(token=token['access_token'],refresh_token=token.get('refresh_token') or secret['refresh_token'],expires_at=time.time()+int(token.get('expires_in',3600)))
        with self.db() as db: db.execute('INSERT OR REPLACE INTO credentials VALUES (?,?)',(cid,json.dumps(secret)))

    def discover(self, cid, items):
        old=self.raw(cid); previous={t['id']:t for t in old['tools']}; tools=[]
        for item in items:
            schema=item.input_schema
            Draft202012Validator.check_schema(schema)
            # Offline validation only: external references must not trigger network reads.
            def local_refs(value):
                if isinstance(value,dict):
                    for k,v in value.items():
                        if k in {'$ref','$dynamicRef'} and isinstance(v,str) and not v.startswith('#'): raise ValueError('工具使用了不支持的远程参数引用。')
                        local_refs(v)
                elif isinstance(value,list):
                    for v in value: local_refs(v)
            local_refs(schema)
            if schema.get('type','object')!='object' or len(json.dumps(schema))>24000: raise ValueError('工具参数定义过大或不是对象。')
            tid='mcp_'+cid+'_'+digest(item.name)[:12]
            fingerprint=digest({'name':item.name,'schema':schema,'description':item.description,'annotations':item.annotations.model_dump() if item.annotations else None})
            prior=previous.get(tid,{})
            default_policy=default_tool_policy(bool(item.annotations and item.annotations.read_only_hint))
            tools.append({'id':tid,'name':item.name,'description':(item.description or '')[:3000], 'schema':schema,
                          'fingerprint':fingerprint,'policy':prior.get('policy',default_policy) if prior.get('fingerprint')==fingerprint else default_policy,
                          'idempotency_parameter':prior.get('idempotency_parameter') if prior.get('fingerprint')==fingerprint else None,
                          'read_hint':bool(item.annotations and item.annotations.read_only_hint)})
        if len({t['id'] for t in tools})!=len(tools): raise ValueError('连接器返回了重复的工具名称。')
        if self.redact_value(cid,tools)!=tools: raise ValueError('工具定义包含认证信息，已拒绝载入。')
        with self.db() as db: db.execute('UPDATE connectors SET tools=? WHERE id=?',(json.dumps(tools),cid))

    def policies(self,cid,policies,idempotency_parameters=None):
        row=self.raw(cid)
        if (set(policies)|set(idempotency_parameters or {}))-{t['id'] for t in row['tools']}: raise ValueError('工具已改变，请刷新连接器。')
        for tool in row['tools']:
            if tool['id'] in policies: tool['policy']=policies[tool['id']]
            if tool['id'] in (idempotency_parameters or {}):
                param=idempotency_parameters[tool['id']]
                if param and tool['schema'].get('properties',{}).get(param,{}).get('type')!='string':
                    raise ValueError('幂等键只能绑定工具定义中的字符串参数。')
                tool['idempotency_parameter']=param
            if tool['policy']!='confirm': tool['idempotency_parameter']=None
        with self.db() as db: db.execute('UPDATE connectors SET tools=? WHERE id=?',(json.dumps(row['tools']),cid))
        return self.public(cid)

    def delete(self,cid):
        self.raw(cid)
        with self.db() as db:
            db.execute('DELETE FROM connectors WHERE id=?',(cid,)); db.execute('DELETE FROM credentials WHERE id=?',(cid,))
            db.execute('DELETE FROM connector_migrations WHERE connector_id=?',(cid,))
        # Retain action receipts for historical runs / duplicate prevention.


class Connection:
    def __init__(self, config):
        self.config=config; self.ready=concurrent.futures.Future(); self.accepting=True
        self.loop=None; self.queue=None
        self.thread=threading.Thread(target=self._run,daemon=True,name='mcp-'+config['id'])
        self.thread.start()

    def _run(self):
        try: asyncio.run(self._serve())
        except BaseException:
            if not self.ready.done(): self.ready.set_exception(ValueError('连接失败，请检查地址、认证或启动配置。'))
        finally:
            self.accepting=False
            if self.queue:
                while not self.queue.empty():
                    item=self.queue.get_nowait()
                    if item and not item[-1].done(): item[-1].set_exception(ValueError('连接已断开。'))

    async def _serve(self):
        c=self.config; self.loop=asyncio.get_running_loop(); self.queue=asyncio.Queue()
        async with AsyncExitStack() as stack:
            if c['transport']=='http':
                client=await stack.enter_async_context(httpx2.AsyncClient(headers={'Authorization':'Bearer '+c['_secret']['token']} if c['_secret'].get('token') else {}, timeout=c['timeout'], follow_redirects=False, trust_env=False))
                streams=await stack.enter_async_context(streamable_http_client(c['url'],http_client=client))
            else:
                errlog=stack.enter_context(open(os.devnull,'w'))
                streams=await stack.enter_async_context(stdio_client(StdioServerParameters(command=c['command'],args=c['args'],env=c['_secret'].get('env',{})),errlog=errlog))
            session=await stack.enter_async_context(ClientSession(*streams, read_timeout_seconds=c['timeout']))
            async with asyncio.timeout(c['timeout']):
                await session.initialize()
                items=[]; cursor=None
                for _ in range(10):
                    page=await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor) if cursor else None)
                    items.extend(page.tools); cursor=page.next_cursor
                    if len(items)>200: raise ValueError('连接器工具超过 200 个。')
                    if not cursor: break
                else: raise ValueError('工具分页超过上限。')
            self.ready.set_result(items)
            while True:
                item=await self.queue.get()
                if item is None: break
                name,args,future=item
                if not self.accepting:
                    future.set_exception(ValueError('连接已断开。')); continue
                try:
                    async with asyncio.timeout(c['timeout']): result=await session.call_tool(name,args)
                    future.set_result(result)
                except Exception:
                    future.set_exception(ValueError('工具调用失败或超时。'))

    def call(self,name,args):
        if not self.accepting or not self.thread.is_alive(): raise ValueError('连接已断开，请重新连接。')
        future=concurrent.futures.Future()
        self.loop.call_soon_threadsafe(self.queue.put_nowait,(name,args,future))
        try: return future.result(self.config['timeout']+2)
        except concurrent.futures.TimeoutError: raise ValueError('工具调用超时。') from None

    def close(self):
        self.accepting=False
        if self.loop and self.queue and not self.loop.is_closed():
            try: self.loop.call_soon_threadsafe(self.queue.put_nowait,None)
            except RuntimeError: pass
        self.thread.join(self.config['timeout']+3)


class Connectors:
    def __init__(self,settings):
        self.store=ConnectorStore(settings); self.connections={}; self.lock=threading.RLock(); self.skill_store=None

    def dependency_status(self,cid):
        row=self.store.raw(cid); deps=row.get('dependencies') or {}; secret=row['_secret']; items=[]
        if row.get('oauth'):
            items.append({'kind':'oauth','name':row['name'],'ready':bool(secret.get('token')),
                          'message':'OAuth 已授权' if secret.get('token') else '需要连接账号'})
        for name in deps.get('executables',[]):
            ready=bool(shutil.which(name));items.append({'kind':'executable','name':name,'ready':ready,'message':'已安装' if ready else '尚未安装'})
        if row['transport']=='stdio' and row.get('package_id') and row['command'] not in deps.get('executables',[]):
            ready=bool(shutil.which(row['command']) or (Path(row['command']).is_absolute() and Path(row['command']).is_file()))
            items.append({'kind':'executable','name':row['command'],'ready':ready,'message':'已安装' if ready else '尚未安装'})
        for name in deps.get('environment',[]):
            ready=name in secret.get('env',{});items.append({'kind':'environment','name':name,'ready':ready,'message':'已配置' if ready else '需要配置环境变量'})
        installed={item['name']:item for item in self.skill_store.list()} if self.skill_store else {}
        for dep in deps.get('skills',[]):
            skill=installed.get(dep['name']);ready=bool(skill and skill['enabled'] and not skill['archived'] and skill['compatible'])
            items.append({'kind':'skill','name':dep['name'],'min_version':dep.get('min_version',''),'ready':ready,
                          'message':'已安装并启用' if ready else '需要安装或启用 Skill'})
        return {'ready':all(item['ready'] for item in items),'items':items}

    def list(self, summary=False):
        with self.lock:
            return [{**c,'status':'connected' if self.connected(c['id']) else 'disconnected',
                     **({} if summary else {'dependency_status':self.dependency_status(c['id'])})} for c in self.store.list(summary=summary)]

    def connected(self,cid):
        c=self.connections.get(cid)
        return bool(c and c.accepting and c.thread.is_alive())

    def connect(self,cid):
        with self.lock:
            self.disconnect(cid)
            status=self.dependency_status(cid)
            if not status['ready']:
                missing='、'.join(item['name'] for item in status['items'] if not item['ready'])
                raise ValueError('连接器依赖尚未就绪：'+missing)
            self.store.refresh_oauth(cid)
            c=Connection(self.store.raw(cid)); self.connections[cid]=c
            try:
                items=c.ready.result(c.config['timeout']+3)
                self.store.discover(cid,items)
            except Exception:
                self.disconnect(cid)
                raise ValueError('连接或工具发现失败，请检查服务地址、认证、参数与服务日志。') from None
            return next(x for x in self.list() if x['id']==cid)

    def disconnect(self,cid):
        with self.lock:
            c=self.connections.pop(cid,None)
            if c: c.close()

    def save(self,body,cid=None):
        with self.lock:
            if cid: self.disconnect(cid)
            return self.store.save(body,cid)

    def preview_package(self,filename,data):
        return self.store.preview_package(filename,data)

    def install_package(self,token):
        with self.lock:return self.store.install_package(token)

    def oauth_start(self,cid,redirect_uri):
        with self.lock:return self.store.oauth_start(cid,redirect_uri)

    def oauth_complete(self,state,code):
        with self.lock:return self.store.oauth_complete(state,code)

    def delete(self,cid):
        with self.lock: self.disconnect(cid); self.store.delete(cid)

    def close(self):
        for cid in list(self.connections): self.disconnect(cid)

    def catalog(self):
        return [{**t,'connector_id':c['id'],'connector_name':c['name'],'source':'mcp','availability':c['status'],
                 'timeout_seconds':c.get('timeout'),
                 'applicability':'对话或伙伴中明确选择后可用。'} for c in self.list() for t in c['tools']]

    def freeze(self,ids):
        if len(ids)>MAX_CONNECTOR_TOOLS_PER_TURN: raise ValueError(f'每轮最多选择 {MAX_CONNECTOR_TOOLS_PER_TURN} 个连接器工具，请减少连接器或调整工具权限。')
        catalog={t['id']:t for t in self.catalog()}; refs=[]
        for tid in dict.fromkeys(ids):
            t=catalog.get(tid)
            if not t or t['policy']=='disabled' or t['availability']!='connected': raise ValueError('所选连接器工具不可用，请连接服务并检查工具权限。')
            refs.append({**t,'revision':self.store.raw(t['connector_id'])['revision']})
        return refs

    def validate(self,ref,args=None):
        with self.lock:
            current=self.freeze([ref['id']])[0]
            if any(current.get(k)!=ref.get(k) for k in ('revision','fingerprint','policy','idempotency_parameter')): raise ValueError('连接器配置或工具权限已改变，请重新发起任务。')
        if args is not None:
            try:
                if len(json.dumps(args))>32000: raise ValueError()
                Draft202012Validator(ref['schema']).validate(args)
            except Exception: raise ValueError('工具参数不符合定义或超过大小限制。') from None
        return current

    def call(self,ref,args):
        self.validate(ref,args)
        with self.lock:
            c=self.connections.get(ref['connector_id'])
            if c is None: raise ValueError('连接已断开。')
        result=c.call(ref['name'],args)
        raw=result.model_dump(mode='json',exclude_none=True,by_alias=True)
        content=[]
        for block in raw.get('content',[]):
            kind=block.get('type')
            if kind=='text': content.append({'type':'text','text':block.get('text','')})
            elif kind=='resource':
                resource=block.get('resource',{})
                content.append({'type':'resource','uri':resource.get('uri'),'text':resource.get('text'), 'mimeType':resource.get('mimeType'),'binary_omitted':'blob' in resource})
            elif kind=='resource_link': content.append({k:block[k] for k in ('type','uri','name','description','mimeType') if k in block})
            else: content.append({'type':kind,'omitted':True})
        data={'content':content,'structuredContent':raw.get('structuredContent')}
        encoded=json.dumps(self.store.redact_value(ref['connector_id'],data),ensure_ascii=False)
        if len(encoded)>20000: data={'text':encoded[:20000], 'truncated':True}
        else: data=json.loads(encoded)
        return {'mcp':{'connector':ref['connector_name'],'tool':ref['name'],**data},'is_error':bool(raw.get('isError'))}

    def write_once(self,ref,args,action_id):
        if ref.get('idempotency_parameter'):
            args={**args,ref['idempotency_parameter']:args.get(ref['idempotency_parameter']) or action_id}
        self.validate(ref,args)
        fingerprint=digest({'tool':ref['id'],'revision':ref['revision'],'args':args})
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM actions WHERE id=?',(action_id,)).fetchone()
            if row:
                if row['fingerprint']!=fingerprint: raise ValueError('操作记录与当前参数不一致。')
                if row['status']=='completed': return json.loads(row['result'])
                raise ValueError('上次写入结果尚未确认；请先在外部服务核对，系统不会自动重试。')
            db.execute('INSERT INTO actions VALUES (?,?,?,NULL)',(action_id,fingerprint,'pending'))
        try: result=self.call(ref,args)
        except Exception:
            with self.store.db() as db: db.execute("UPDATE actions SET status='unknown' WHERE id=?",(action_id,))
            raise ValueError('外部操作结果未知，请到目标服务核对；系统不会自动重试。') from None
        with self.store.db() as db: db.execute("UPDATE actions SET status='completed', result=? WHERE id=?",(json.dumps(result,ensure_ascii=False),action_id))
        return result

    def tools(self,refs):
        def build(ref):
            def invoke(**kwargs):
                if ref['policy']!='read': raise ValueError('此工具必须通过确认流程调用。')
                try: return self.call(ref,kwargs)
                except ValueError as exc: return {'is_error':True,'error':str(exc)}
            return StructuredTool.from_function(invoke,name=ref['id'],description=ref['connector_name']+' / '+ref['name']+': '+ref['description'],args_schema=ref['schema'], metadata={'mcp_ref':ref})
        return [build(ref) for ref in refs]
