"""只监听本机的 FastAPI 应用：资料、会话、任务事件与笔记接口。"""

import asyncio
import json
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from personal_workbench.context_engine import TaskEdit, RebuildInput, ExclusionInput, ContextEngine
from personal_workbench.assistant_service import open_service
from personal_workbench.library import Library, UPLOAD_LIMIT
from personal_workbench.settings import Settings
from personal_workbench.connector_limits import MAX_CONNECTOR_TOOLS_PER_TURN
from personal_workbench.app_settings import AppSettings, ModelSettingsInput, ModelProfile, LanguageInput, configured
from personal_workbench.knowledge import KnowledgeCenter
from personal_workbench.rag_config import EngineConfig, KnowledgeInput
from personal_workbench.web_jobs import Jobs
from personal_workbench.chat_attachments import MAX_IMAGE_BYTES
from personal_workbench.workspace import ARTIFACT_SUFFIXES, MAX_ARTIFACT_BYTES, Workspace, read_bytes
from personal_workbench.partner_store import PartnerStore, PartnerInput, PartnerStatus
from personal_workbench.assistant_profile import AssistantProfileStore, WorkbenchAssistantInput
from personal_workbench.capabilities.partner import register_partners, partner_catalog
from personal_workbench.workflows.definition import WorkflowInput, templates as workflow_templates
from personal_workbench.workflows.store import WorkflowStore
from personal_workbench.capability_studio import CapabilityStudio, Preview, Import as PackageImport, Commit, Suggest, Kind
from personal_workbench.teams.definition import TeamInput, templates as team_templates
from personal_workbench.teams.store import TeamStore
from personal_workbench.teams.runtime import TeamCapability, register_teams
from personal_workbench.workflows.runtime import FixedWorkflowCapability, register_workflows, workflow_catalog
from personal_workbench.file_tools import TOOL_INFO
from personal_workbench.skill_runtime import MAX_SKILLS_PER_ACTOR
from personal_workbench.evaluation import EvaluationService, EvaluateRequest, CompareRequest
from personal_workbench.projects import ProjectStore, ConversationDeletionService


class SkillRef(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class SkillActivationInput(BaseModel):
    model_config={'extra':'forbid'}
    auto: bool = True
    user_invocable: bool = True
    internal_only: bool = False
    keywords: list[str] = Field(default_factory=list,max_length=30)
    priority: int = Field(default=50,ge=0,le=100)


class SkillImport(BaseModel):
    preview_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    target_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    display_name: str = Field(default="", max_length=80)
    allowed_tools: list[str] = Field(max_length=len(TOOL_INFO))
    activation: SkillActivationInput | None = None
    allow_scripts: bool = False
    accept_risk: bool = False


class SkillUpdate(BaseModel):
    enabled: bool | None = None
    archived: bool | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    allowed_tools: list[str] | None = Field(default=None, max_length=len(TOOL_INFO))
    auto_trigger: bool | None = None
    user_invocable: bool | None = None
    internal_only: bool | None = None
    scripts_enabled: bool | None = None


class RemoteSkillPreview(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


class SkillProposalDecision(BaseModel):
    allowed_tools: list[str] = Field(default_factory=list, max_length=len(TOOL_INFO))
    accept_risk: bool = False


class ConnectorPackageInstall(BaseModel):
    preview_id: str = Field(pattern=r'^[a-f0-9]{32}$')


from personal_workbench.connectors import Connectors, ConnectorInput, ToolPolicies
from personal_workbench.web_tools.config import WebToolsConfig
from personal_workbench.decision_tools.config import DecisionToolsConfig
from personal_workbench.web_tools.sources import WebSources
from personal_workbench.memory.config import MemoryConfig, MemorySelection, SpaceInput, MemoryInput, MemoryEdit, RecallInput
from personal_workbench.memory.rule_schema import RuleProposal, RuleAction
from personal_workbench.memory.config import ScopeInput, MemoryStateInput, ReviewInput


class MemoryProfileInput(BaseModel):
    profile_id: Literal["langmem-default", "mem0-default"]


class FreshMemorySpace(SpaceInput):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")


class MemoryActivationInput(MemoryProfileInput):
    space_id: str | None = Field(default=None,pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    new_space: FreshMemorySpace | None = None


class Ask(BaseModel):
    memory: MemorySelection | None = None
    connector_tool_ids: list[Annotated[str, Field(pattern=r"^mcp_[a-f0-9]{16}_[a-f0-9]{12}$")]] = Field(default_factory=list, max_length=MAX_CONNECTOR_TOOLS_PER_TURN)
    skill_refs: list[SkillRef] = Field(default_factory=list, max_length=MAX_SKILLS_PER_ACTOR)
    capability_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    capability_version: str | None = Field(default=None, max_length=32)
    model_profile_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    kb_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    kb_ids: list[Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]] | None = Field(default=None, max_length=50)
    text: str = Field(min_length=1, max_length=10000)
    thread_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    project_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    attachment_ids: list[Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def scope(self):
        if self.kb_id is not None and self.kb_ids is not None:
            raise ValueError("请只传入 kb_ids，不能同时指定 kb_id。")
        if self.kb_ids is not None:
            self.kb_ids = list(dict.fromkeys(self.kb_ids))
        return self


class Resume(BaseModel):
    decision: bool | Literal['session'] | None = None


class Note(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=32000)
    thread_id: str | None = None


class ConversationUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    title: str | None = Field(default=None, min_length=1, max_length=120)
    pinned: bool | None = None
    project_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class ProjectDefinition(BaseModel):
    model_config = {"extra": "forbid"}
    name: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def clean(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("请填写项目名称。")
        return self


class ProjectPatch(BaseModel):
    model_config = {"extra": "forbid"}
    name: str | None = Field(default=None, min_length=1, max_length=80)
    pinned: bool | None = None

    @model_validator(mode="after")
    def clean(self):
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("请填写项目名称。")
        if "pinned" in self.model_fields_set and self.pinned is None:
            raise ValueError("请明确是否置顶项目。")
        if self.name is not None:
            self.name = self.name.strip()
            if not self.name:
                raise ValueError("请填写项目名称。")
        return self


def create_app(settings=None, model_override=None, embedding_factory=None, memory_engine_factory=None):
    settings = settings or Settings.load()
    preferences = AppSettings(settings)
    settings = preferences.runtime()
    library = Library(settings)
    library.sync_local()
    # 先完成 M1 数据库的兼容迁移，再允许并发读取。
    with open_service(settings):
        pass
    knowledge = KnowledgeCenter(settings, embedding_factory)
    knowledge.app_settings = preferences
    jobs = Jobs(settings, library, model_override, knowledge)
    jobs.app_settings = preferences
    projects = ProjectStore(settings)
    attachments = jobs.assistant.attachments
    conversation_deletion = ConversationDeletionService(settings, projects, jobs, attachments)
    memory = jobs.assistant.memory
    from personal_workbench.memory.workspace import WorkspaceMemory
    memory_workspace = WorkspaceMemory(memory)
    memory_workspace.initialize()
    if memory_engine_factory:
        memory.engine_factory = memory_engine_factory
    connectors = Connectors(settings)
    jobs.assistant.connectors = connectors
    assistant_profiles = AssistantProfileStore(settings)
    jobs.assistant.profile_store = assistant_profiles
    partners = PartnerStore(settings)
    register_partners(jobs, partners)
    workflows = WorkflowStore(settings)
    register_workflows(jobs, workflows, partners)
    teams = TeamStore(settings)
    register_teams(jobs, teams, partners)

    from personal_workbench.memory.learning import LearningWorker
    learning_worker = LearningWorker(jobs.assistant.learning)
    from personal_workbench.memory.mem0_native import NativeWorker
    memory.native_mem0.bootstrap_empty()
    mem0_worker=NativeWorker(memory.native_mem0)
    from personal_workbench.memory.mem0_routes import install as install_mem0_routes

    @asynccontextmanager
    async def lifespan(app):
        learning_worker.start()
        mem0_worker.start()
        yield
        learning_worker.close()
        mem0_worker.close()
        await asyncio.to_thread(jobs.close)
        await asyncio.to_thread(knowledge.close)
        await asyncio.to_thread(connectors.close)

    app = FastAPI(title="个人学习工作台", lifespan=lifespan)
    app.state.library, app.state.jobs = library, jobs
    app.state.projects = projects
    app.state.knowledge = knowledge
    app.state.preferences = preferences
    app.state.attachments = attachments
    skills = jobs.assistant.skills
    connectors.skill_store = skills
    app.state.skills = skills
    app.state.partners = partners
    app.state.assistant_profiles = assistant_profiles
    app.state.workflows = workflows
    app.state.teams = teams
    studio = CapabilityStudio(jobs,partners,workflows,teams,preferences,knowledge,connectors)
    app.state.studio = studio
    app.state.connectors = connectors
    app.state.memory = memory
    app.state.learning = jobs.assistant.learning
    evaluations = EvaluationService(settings,jobs)
    app.state.evaluations = evaluations
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            allowed = {str(request.base_url).rstrip("/"), "http://127.0.0.1:5173", "http://localhost:5173"}
            if (origin and origin not in allowed) or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "只接受本机工作台页面发起的操作。"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(ValueError)
    async def bad_request(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        if request.url.path.startswith(('/api/workflows','/api/teams')):
            detail = '流程配置无效：' + '；'.join('.'.join(str(p) for p in e['loc'][1:])+': '+e['msg'] for e in exc.errors())
            return JSONResponse({'detail':detail},status_code=422)
        # Pydantic 默认错误包含整个 input；配置可能携带 API Key，不能回显。
        return JSONResponse({"detail": "输入无效，请检查必填项、数字范围、分块重叠和服务地址。"}, status_code=422)

    @app.get("/api/skill-center")
    def skill_center(compact: bool = False):
        from personal_workbench.file_tools import builtin_tool_catalog
        from personal_workbench.tool_catalog import compact_connections
        items = connectors.list(summary=compact)
        builtin = builtin_tool_catalog()
        return {"partners": partner_catalog(jobs, partners) + workflow_catalog(jobs, workflows) + workflow_catalog(jobs, teams), "skills": skills.list(),
                "tools": [{k:v for k,v in tool.items() if k != 'schema'} for tool in builtin] if compact else builtin,
                "tool_count":len(builtin)+sum(len(c['tools']) for c in items),
                "connectors": compact_connections(items) if compact else items}

    @app.get('/api/tools')
    def list_tools(q: str = Query(default='', max_length=200), source: Literal['all','builtin','mcp'] = 'all',
                   connector_id: str | None = Query(default=None, max_length=64),
                   policy: Literal['all','contextual','disabled','read','confirm'] = 'all',
                   status: Literal['all','available','connected','disconnected','contextual'] = 'all',
                   page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=50)):
        if page_size not in {20,50}: raise HTTPException(status_code=422, detail='每页工具数量只支持 20 或 50。')
        from personal_workbench.tool_catalog import page as tool_page
        return tool_page(connectors, q=q, source=source, connector_id=connector_id, policy=policy,
                         availability=status, page=page, page_size=page_size)

    @app.get('/api/tools/{tool_id}')
    def tool_detail(tool_id: str):
        from personal_workbench.tool_catalog import detail
        result = detail(connectors, tool_id)
        if result is None: raise HTTPException(status_code=404, detail='工具已移除或定义已更新，请刷新列表。')
        return result

    @app.post('/api/capability-studio/preview')
    def preview_configuration(body: Preview):
        return studio.preview(body)

    @app.post('/api/capability-studio/import')
    def preview_package(body: PackageImport):
        return studio.import_package(body)

    @app.post('/api/capability-studio/commit')
    def commit_configuration(body: Commit):
        return studio.commit(body.preview_id)

    @app.get('/api/capability-studio/{kind}/{identifier}/export')
    def export_configuration(kind: Kind, identifier: str, version: str | None = None):
        return studio.export(kind,identifier,version)

    @app.get('/api/capability-studio/{kind}/{identifier}/versions')
    def configuration_versions(kind: Kind, identifier: str):
        return studio.revisions(kind,identifier)

    @app.post('/api/capability-studio/suggest')
    def suggest_capability(body: Suggest):
        return studio.suggestions(body)

    @app.get('/api/partners')
    def list_partners():
        return partner_catalog(jobs, partners) + workflow_catalog(jobs, workflows) + workflow_catalog(jobs, teams)

    def validate_assistant_profile(body):
        for ref in body.skill_refs:
            skills.revision(ref.id, ref.revision)
        if body.model_profile_id:
            preferences.profile(body.model_profile_id)
        for kid in body.suggested_kb_ids:
            knowledge.row(kid)
        available = {tool['id'] for tool in connectors.catalog()}
        if set(body.connector_tool_ids) - available:
            raise ValueError('所选连接器工具不存在，请刷新配置。')

    @app.get('/api/workbench-assistant')
    def workbench_assistant_profile():
        return assistant_profiles.get()

    @app.put('/api/workbench-assistant')
    def save_workbench_assistant(body: WorkbenchAssistantInput):
        with jobs.lock:
            validate_assistant_profile(body)
            return assistant_profiles.save(body)

    @app.post('/api/workbench-assistant/reset')
    def reset_workbench_assistant():
        with jobs.lock:
            return assistant_profiles.reset()

    @app.post('/api/workbench-assistant/restore/{profile_version}')
    def restore_workbench_assistant(profile_version: str):
        with jobs.lock:
            body = assistant_profiles.input_for(profile_version)
            validate_assistant_profile(body)
            return assistant_profiles.save(body)

    def save_partner(body, pid=None):
        with jobs.lock:
            for ref in body.skill_refs: skills.revision(ref.id, ref.revision)
            if body.model_profile_id: preferences.profile(body.model_profile_id)
            for kid in body.suggested_kb_ids: knowledge.row(kid)
            available = {t['id'] for t in connectors.catalog()}
            if set(body.connector_tool_ids)-available: raise ValueError('所选连接器工具不存在，请刷新配置。')
            result = partners.save(body, pid)
            register_partners(jobs, partners)
            return result

    @app.post('/api/partners')
    def create_partner(body: PartnerInput):
        return save_partner(body)

    @app.put('/api/partners/{pid}')
    def edit_partner(pid: str, body: PartnerInput):
        return save_partner(body, pid)

    @app.patch('/api/partners/{pid}')
    def partner_status(pid: str, body: PartnerStatus):
        with jobs.lock:
            return partners.status(pid, body.model_dump(exclude_none=True))

    @app.get('/api/teams/templates')
    def expert_team_templates():
        return team_templates()

    @app.get('/api/teams')
    def list_teams():
        return teams.list()

    def save_team(body, tid=None):
        with jobs.lock:
            definition = {**body.model_dump(), 'id':tid or 'draft', 'version':'draft'}
            capability = TeamCapability(jobs.assistant,teams,partners,definition)
            for expert in capability.resolve_experts().values():
                for ref in expert.get('skill_refs',[]): skills.revision(ref['id'],ref['revision'])
                if set(expert.get('connector_tool_ids',[])) - {t['id'] for t in connectors.catalog()}:
                    raise ValueError('团队引用的连接器工具不存在。')
            result = teams.save(body,tid)
            register_teams(jobs,teams,partners)
            return result

    @app.post('/api/teams/validate')
    def validate_team(body: TeamInput):
        return body.model_dump()

    @app.post('/api/teams')
    def create_team(body: TeamInput):
        return save_team(body)

    @app.put('/api/teams/{tid}')
    def edit_team(tid: str, body: TeamInput):
        return save_team(body,tid)

    @app.patch('/api/teams/{tid}')
    def team_status(tid: str, body: PartnerStatus):
        with jobs.lock:
            return teams.status(tid,body.model_dump(exclude_none=True))

    @app.get('/api/workflows/templates')
    def flow_templates():
        return workflow_templates()

    @app.get('/api/workflows')
    def list_workflows():
        return workflows.list()

    @app.post('/api/workflows/{wid}/tool-preview')
    def workflow_tool_preview(wid: str, body: Ask):
        # Reuse execution preparation without creating a job, invoking a model,
        # or returning the private runtime/model configuration.
        with jobs.lock:
            version = body.capability_version or workflows.get(wid)['version']
            capability = jobs.registry.resolve(wid,version)
            if not isinstance(capability,FixedWorkflowCapability):
                raise ValueError('此伙伴不是固定流程。')
            prepared = capability.prepare({**body.model_dump(),'thread_id':'tool-preview',
                'existing':False,'resume':False,'decision':None,'run_id':'tool-preview'})
            return capability.tool_preview(prepared)

    def save_workflow(body, wid=None):
        with jobs.lock:
            definition = {**body.model_dump(), 'id':wid or 'draft', 'version':'draft'}
            capability = FixedWorkflowCapability(jobs.assistant,workflows,partners,definition)
            for expert in capability.resolve_experts().values():
                for ref in expert.get('skill_refs',[]): skills.revision(ref['id'],ref['revision'])
                if set(expert.get('connector_tool_ids',[])) - {t['id'] for t in connectors.catalog()}:
                    raise ValueError('流程引用的连接器工具不存在。')
            result = workflows.save(body,wid)
            register_workflows(jobs,workflows,partners)
            return result

    @app.post('/api/workflows/validate')
    def validate_workflow(body: WorkflowInput):
        return body.model_dump()

    @app.post('/api/workflows')
    def create_workflow(body: WorkflowInput):
        return save_workflow(body)

    @app.put('/api/workflows/{wid}')
    def edit_workflow(wid: str, body: WorkflowInput):
        return save_workflow(body,wid)

    @app.patch('/api/workflows/{wid}')
    def workflow_status(wid: str, body: PartnerStatus):
        with jobs.lock:
            return workflows.status(wid,body.model_dump(exclude_none=True))

    @app.get('/api/connectors')
    def list_connectors(summary: bool = False):
        from personal_workbench.tool_catalog import compact_connections
        items=connectors.list(summary=summary)
        return compact_connections(items) if summary else items

    @app.post('/api/connectors/packages/preview')
    async def preview_connector_package(file: UploadFile):
        from personal_workbench.connector_package import UPLOAD_LIMIT as CONNECTOR_PACKAGE_LIMIT
        try:
            data=await file.read(CONNECTOR_PACKAGE_LIMIT+1)
            return await asyncio.to_thread(connectors.preview_package,file.filename or '',data)
        finally: await file.close()

    @app.post('/api/connectors/packages/install')
    def install_connector_package(body: ConnectorPackageInstall):
        return connectors.install_package(body.preview_id)

    @app.post('/api/connectors/{cid}/oauth/start')
    def start_connector_oauth(cid: str, request: Request):
        redirect=str(request.url_for('connector_oauth_callback'))
        return connectors.oauth_start(cid,redirect)

    @app.get('/api/connectors/oauth/callback',name='connector_oauth_callback')
    def connector_oauth_callback(state: str = Query(min_length=10,max_length=300), code: str = Query(min_length=1,max_length=8000)):
        connectors.oauth_complete(state,code)
        return HTMLResponse('<!doctype html><meta charset="utf-8"><title>OAuth complete</title><p>授权已完成，可以关闭此窗口。</p><script>window.opener&&window.opener.postMessage("workbench-oauth-complete",location.origin);window.close()</script>',
                            headers={'Content-Security-Policy':"default-src 'none'; script-src 'unsafe-inline'; style-src 'none'"})

    @app.post('/api/connectors')
    def create_connector(body: ConnectorInput):
        return connectors.save(body)

    @app.put('/api/connectors/{cid}')
    def edit_connector(cid: str, body: ConnectorInput):
        return connectors.save(body, cid)

    @app.post('/api/connectors/{cid}/connect')
    def connect_connector(cid: str):
        return connectors.connect(cid)

    @app.post('/api/connectors/{cid}/disconnect')
    def disconnect_connector(cid: str):
        connectors.disconnect(cid)
        return {'status':'disconnected'}

    @app.put('/api/connectors/{cid}/tools')
    def connector_policies(cid: str, body: ToolPolicies):
        with connectors.lock:
            return connectors.store.policies(cid, body.policies, body.idempotency_parameters)

    @app.delete('/api/connectors/{cid}')
    def delete_connector(cid: str):
        connectors.delete(cid)
        return {'deleted':True}

    @app.get("/api/skills")
    def list_skills():
        from personal_workbench.external_skills import sync_external_skills
        sync_external_skills(skills)
        return skills.list()

    @app.post("/api/skills/preview")
    async def preview_skill(file: UploadFile):
        from personal_workbench.skill_package import UPLOAD_LIMIT as SKILL_LIMIT
        try:
            data = await file.read(SKILL_LIMIT + 1)
            filename = file.filename or ""
            return await asyncio.to_thread(skills.preview, filename, data, {
                'kind':'local', 'label':'本地文件', 'identifier':filename, 'trust_level':'local',
            })
        finally:
            await file.close()

    @app.post("/api/skills/github/preview")
    async def preview_github_skill(body: RemoteSkillPreview):
        from personal_workbench.skill_sources import fetch_skill_source
        filename,data,source = await asyncio.to_thread(fetch_skill_source,'github',body.url)
        return await asyncio.to_thread(skills.preview,filename,data,source)

    @app.post("/api/skills/url/preview")
    async def preview_url_skill(body: RemoteSkillPreview):
        from personal_workbench.skill_sources import fetch_skill_source
        filename,data,source = await asyncio.to_thread(fetch_skill_source,'url',body.url)
        return await asyncio.to_thread(skills.preview,filename,data,source)

    @app.post("/api/skills/examples/{name}/preview")
    def example_skill(name: str):
        import io
        import zipfile
        from personal_workbench.bundled_skills import BUNDLED_SKILLS
        if name not in BUNDLED_SKILLS: raise ValueError("内置技能不存在。")
        folder = Path(__file__).resolve().parents[2]/"bundled"/"skills"/name
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer,"w") as archive:
            for path in sorted(folder.rglob("*")):
                if path.is_file(): archive.write(path, str(path.relative_to(folder)))
        return skills.preview(name+".zip",buffer.getvalue(),{
            'kind':'example','label':'内置技能','identifier':name,'trust_level':'builtin',
        })

    @app.post("/api/skills/import")
    def import_skill(body: SkillImport):
        with jobs.lock:
            return skills.commit(body.preview_id,body.allowed_tools,body.display_name,body.target_id,
                                 body.activation.model_dump() if body.activation else None,body.allow_scripts,
                                 body.accept_risk)

    @app.get("/api/skills/{sid}")
    def skill_detail(sid: str):
        return {**skills.get(sid), 'used_by':partners.references(sid), 'audit':skills.audit(sid)}

    @app.get("/api/skills/{sid}/audit")
    def skill_audit(sid: str):
        return skills.audit(sid)

    @app.post("/api/skills/{sid}/check-update")
    async def check_skill_update(sid: str):
        from personal_workbench.skill_sources import refetch_skill_source
        current=skills.get(sid)
        filename,data,source=await asyncio.to_thread(refetch_skill_source,current.get('source') or {})
        candidate=await asyncio.to_thread(skills.preview,filename,data,source)
        if candidate['revision']==current['revision']:
            skills.discard_preview(candidate['preview_id'])
            return {'status':'up_to_date','current_revision':current['revision']}
        return {'status':'update_available','current_revision':current['revision'],
                'diff':skills.version_diff(current,candidate),'preview':candidate}

    @app.post("/api/skills/{sid}/revisions/{revision}/restore")
    def restore_skill_revision(sid: str, revision: str):
        with jobs.lock:
            return skills.restore_revision(sid,revision)

    @app.get("/api/skills/proposals/pending")
    def pending_skill_proposals():
        from personal_workbench.skill_proposals import SkillProposalStore
        return SkillProposalStore(settings).list('pending')

    @app.post("/api/skills/proposals/{proposal_id}/approve")
    def approve_skill_proposal(proposal_id: str, body: SkillProposalDecision):
        from personal_workbench.skill_proposals import SkillProposalStore
        with jobs.lock:
            return SkillProposalStore(settings).approve(proposal_id,body.allowed_tools,body.accept_risk)

    @app.post("/api/skills/proposals/{proposal_id}/reject")
    def reject_skill_proposal(proposal_id: str):
        from personal_workbench.skill_proposals import SkillProposalStore
        return SkillProposalStore(settings).reject(proposal_id)

    @app.patch("/api/skills/{sid}")
    def update_skill(sid: str, body: SkillUpdate):
        with jobs.lock:
            return skills.update(sid,body.model_dump(exclude_none=True))

    @app.get("/api/skills/{sid}/revisions/{revision}/file")
    def skill_file(sid: str, revision: str, path: str):
        content, sha = skills.resource(sid,revision,path)
        return {"path":path,"content":content,"sha256":sha}

    @app.get("/api/config")
    def config():
        active_jobs = jobs.active_jobs()
        return {"model": settings.model, "configured": configured(settings), "notes_dir": str(settings.notes_dir), "provider": settings.provider,
                "active_job": active_jobs[0] if active_jobs else None, "active_jobs": active_jobs}

    @app.get("/api/settings")
    def get_settings():
        return preferences.public()

    install_mem0_routes(app,memory)
    from personal_workbench.memory.mem0_p7_routes import install as install_mem0_p7_routes
    install_mem0_p7_routes(app,memory)

    @app.get('/api/memory')
    def memory_center():
        from importlib.metadata import version
        spaces = memory.store.spaces()
        return {'config':memory.store.config(), 'configs':{e:memory.store.config(e) for e in ('langmem','mem0')}, **memory.store.registry.state(), 'spaces':spaces,
                **memory_workspace.state(),
                'engines':[{'id':eid, 'name':name, 'version':version(package), 'local':True}
                           for eid,name,package in [('langmem','LangMem','langmem'),('mem0','Mem0 OSS','mem0ai')]]}

    @app.get('/api/memory/spaces/{sid}/learning')
    def learning_jobs(sid: str, page: int = Query(default=1,ge=1,le=100000)):
        return jobs.assistant.learning.public(sid,page)

    @app.post('/api/memory/spaces/{sid}/learning/flush')
    def flush_learning(sid: str):
        result = jobs.assistant.learning.action(sid,'flush')
        learning_worker.wake.set()
        return result

    @app.post('/api/memory/spaces/{sid}/learning/{jid}/{action}')
    def manage_learning(sid: str,jid: str,action: Literal['retry','cancel']):
        result = jobs.assistant.learning.action(sid,action,jid)
        learning_worker.wake.set()
        return result

    @app.post('/api/memory/activate')
    def activate_memory(body: MemoryActivationInput):
        with jobs.lock:
            if jobs.active:
                raise ValueError('请等待当前任务完成后再切换记忆方案。')
            # Shares the CLI execution lock. No switch during tools/approval/resume.
            with open_service(settings, library=library, knowledge=knowledge) as service:
                for row in service.sessions():
                    if row['status'] in {'waiting_approval', 'running', 'interrupted', 'stopped', 'failed'}:
                        if service.status(row['id'])['next']:
                            raise ValueError('请先完成或恢复未结束的任务，再切换记忆方案。')
                if body.space_id and memory.store.space(body.space_id)["memory_profile_id"]!=body.profile_id:
                    raise ValueError("记忆空间不属于目标引擎。")
                return memory.activate(body.profile_id, body.new_space.model_dump() if body.new_space else None, body.space_id)

    @app.put('/api/memory/default')
    def default_memory(body: MemoryProfileInput):
        return memory.store.registry.set_default(body.profile_id)

    @app.put('/api/memory/config')
    def memory_config(body: MemoryConfig):
        if body.enabled and body.learn_memories:
            runtime = preferences.runtime(body.mem0.model_profile_id if body.engine == "mem0" else body.model_profile_id)
            if not configured(runtime):
                raise ValueError('请先配置记忆提取模型。')
        elif body.model_profile_id:
            preferences.profile(body.model_profile_id)
        if body.mem0.model_profile_id:
            preferences.profile(body.mem0.model_profile_id)
        if body.mem0.embedding_profile_id:
            preferences.profile(body.mem0.embedding_profile_id, kind='embedding')
        if body.langmem.embedding_profile_id:
            preferences.profile(body.langmem.embedding_profile_id, kind='embedding')
        return memory.store.save_config(body)

    @app.post('/api/memory/probe')
    def memory_probe(body: MemoryConfig):
        return memory.probe(body)

    @app.delete('/api/memory/spaces/{sid}')
    def delete_memory_space(sid: str):
        with jobs.lock:
            if jobs.active: raise ValueError('请等待当前任务完成后再删除记忆空间。')
            with open_service(settings, library=library, knowledge=knowledge) as service:
                for row in service.sessions():
                    if row['status'] in {'waiting_approval','running','interrupted','stopped','failed'} and service.status(row['id'])['next']:
                        raise ValueError('请先完成或恢复未结束的任务，再删除记忆空间。')
                return memory_workspace.delete(sid)

    from personal_workbench.memory.episode_schema import EpisodeEdit, EpisodeState, EpisodeMerge, EpisodeSessionInput

    @app.get('/api/memory/spaces/{sid}/rules')
    def list_rules(sid: str, scope_kind: Literal['personal','project','partner']='personal', scope_id: str='personal'):
        return memory.rules.listing(sid,(scope_kind,scope_id))

    @app.post('/api/memory/spaces/{sid}/rules',status_code=202)
    def propose_rule(sid: str, body: RuleProposal):
        result=memory.rules.propose(sid,body);learning_worker.wake.set();return result

    @app.get('/api/memory/rules/{rid}')
    def rule_detail(rid: str):
        return memory.rules.detail(rid)

    @app.post('/api/memory/rules/{rid}/evaluate',status_code=202)
    def evaluate_rule(rid: str, body: RuleAction):
        result=memory.rules.evaluate(rid,body);learning_worker.wake.set();return result

    @app.post('/api/memory/rules/{rid}/activate')
    def activate_rule(rid: str, body: RuleAction):
        with jobs.lock:
            if jobs.active:raise ValueError('请等待当前对话完成后再启用协作规则。')
            return memory.rules.activate(rid,body)

    @app.post('/api/memory/rules/{rid}/reject')
    def reject_rule(rid: str, body: RuleAction):
        return memory.rules.reject(rid,body)

    @app.post('/api/memory/rules/{rid}/disable')
    def disable_rule(rid: str, body: RuleAction):
        with jobs.lock:
            if jobs.active:raise ValueError('请等待当前对话完成后再停用协作规则。')
            return memory.rules.disable(rid,body)

    @app.post('/api/memory/rule-jobs/{jid}/{action}')
    def rule_job_action(jid: str, action: Literal['retry','cancel']):
        result=memory.rules.job_action(jid,action);learning_worker.wake.set();return result

    @app.get('/api/memory/spaces/{sid}/episodes')
    def episode_list(sid: str, scope_kind: str='personal', scope_id: str='personal', q: str=Query(default='',max_length=200), status: str='active', outcome: str='', page: int=Query(default=1,ge=1,le=100000)):
        return memory.episodes.listing(sid,(scope_kind,scope_id),q,status,outcome,page)

    @app.get('/api/memory/episodes/{eid}')
    def episode_detail(eid: str,page: int=Query(default=1,ge=1,le=100000)):
        return memory.episodes.detail(eid,page)

    @app.get('/api/memory/episodes/{eid}/artifacts/{event_id}')
    def episode_artifact(eid: str,event_id: str):
        return memory.episodes.artifact(eid,event_id)

    @app.put('/api/memory/episodes/{eid}')
    def episode_edit(eid: str,body: EpisodeEdit):
        return memory.episodes.edit(eid,body)

    @app.put('/api/memory/episodes/{eid}/state')
    def episode_state(eid: str,body: EpisodeState):
        return memory.episodes.state(eid,body)

    @app.post('/api/memory/episodes/{eid}/merge')
    def episode_merge(eid: str,body: EpisodeMerge):
        return memory.episodes.merge(eid,body)

    @app.post('/api/memory/spaces/{sid}/episode-jobs/{jid}/{action}')
    def episode_job(sid: str,jid: str,action: Literal['retry','cancel','flush']):
        result=memory.episodes.job_action(sid,jid,action);learning_worker.wake.set();return result

    @app.post('/api/memory/spaces/{sid}/episodes/recall')
    def episode_recall(sid: str,body: RecallInput):
        frozen=memory.freeze({'space_id':sid,'scope_kind':body.scope_kind,'scope_id':body.scope_id})
        return {'items':memory.episodes.select(frozen,body.query)}

    def capture_episode(tid,body,*,new_task=False,completion=False,already_locked=False):
        with nullcontext() if already_locked else jobs.lock:
            if jobs.active:raise ValueError('请等待当前对话完成后再操作经历。')
            with open_service(settings,read_only=True,library=library,knowledge=knowledge) as service:
                service.require_working_memory(tid)
                current=service.status(tid)
                if current['next']:raise ValueError('请先完成或恢复当前任务，再保存经历。')
                binding=json.loads(service.session(tid)['skill_snapshot'] or '{}').get('memory') or {}
                if binding.get('engine')!='langmem' or not binding.get('enabled',True):raise ValueError('此会话未启用 LangMem 方案。')
                selection={**binding.get('selection',{}),**body.model_dump()}
                if selection['space_id']!=binding['space_id'] or (selection['scope_kind'],selection['scope_id'])!=(binding.get('scope_kind','personal'),binding.get('scope_id','personal')):
                    raise ValueError('请选择本轮实际使用的记忆空间和范围，下一轮后再更换。')
                frozen=memory.freeze(selection)
                if new_task:return memory.episodes.new_task(frozen,tid)
                state=current['state'];messages=state.get('messages',[])
                start=max((i for i,m in enumerate(messages) if m.type=='human'),default=-1)
                if start<0:raise ValueError('当前会话还没有可保存的来源。')
                user=messages[start]
                if user.id in state.get('working_excluded_turns',[]):raise ValueError('此轮已被排除，请先恢复来源。')
                run=user.additional_kwargs.get('run_id') or state['turn_id']
                if completion:run += ':task:'+str(state.get('task_state',{}).get('version',0))
                jid=memory.episodes.begin(frozen,user.text,tid,run,manual=not completion)
                if not jid:
                    if completion:return {'skipped':True}
                    raise ValueError('此轮包含不记录要求或敏感标记，未保存经历。')
                with memory.episodes.store.connect() as db:
                    row=db.execute('SELECT * FROM episode_runs WHERE id=?',(jid,)).fetchone()
                    if row['state'] in ('skipped','cancelled'):raise ValueError('此轮来源已跳过或取消，请从新的提问开始。')
                    if row['state']=='completed':return {'id':row['task_id'],'status':'completed'}
                    if not completion and row['state'] in ('idle','collecting','pending','retry_wait','failed'):
                        db.execute("UPDATE episode_runs SET manual=1,frozen=?,epoch=?,state='collecting' WHERE id=?",(json.dumps(frozen),memory.episodes.store.epoch(body.space_id),jid))
                memory.episodes.observe(jid,messages[start+1:])
                if completion:
                    with memory.episodes.store.connect() as db:
                        memory.episodes.event(db,jid,'task-completed:'+str(state.get('task_state',{}).get('version',0)),'user','task_completion','用户在当前任务面板明确标记任务完成。',status='success')
                memory.episodes.finish(jid,current['status'],explicit=True)
                learning_worker.wake.set()
                return {'id':row['task_id'],'job_id':jid,'status':'queued'}

    @app.post('/api/sessions/{tid}/episodes/save')
    def save_episode(tid: str,body: EpisodeSessionInput):
        return capture_episode(tid,body)

    @app.post('/api/sessions/{tid}/episodes/new-task')
    def new_episode_task(tid: str,body: EpisodeSessionInput):
        return capture_episode(tid,body,new_task=True)

    from personal_workbench.memory.lifecycle import RetentionInput, DeleteInput

    @app.get('/api/memory/spaces/{sid}/lifecycle')
    def memory_lifecycle(sid: str, page: int = Query(default=1,ge=1), kind: str = 'all'):
        return memory.lifecycle.dashboard(sid,page,kind)

    @app.get('/api/memory/spaces/{sid}/objects/{oid}/impact')
    def memory_impact(sid: str, oid: str):
        return memory.lifecycle.preview(sid,oid)

    @app.delete('/api/memory/spaces/{sid}/objects/{oid}')
    def memory_forget(sid: str, oid: str, body: DeleteInput):
        with jobs.lock:
            if jobs.active:raise ValueError('请等待当前对话任务结束后再删除记忆。')
            return memory.lifecycle.delete(sid,oid,body.token)

    @app.put('/api/memory/spaces/{sid}/objects/{oid}/retention')
    def memory_retention(sid: str, oid: str, body: RetentionInput):
        with jobs.lock:
            if jobs.active:raise ValueError('请等待当前对话任务结束后再调整记忆期限。')
            return memory.lifecycle.retention(sid,oid,body)

    @app.post('/api/memory/spaces/{sid}/index/continue')
    def memory_index_continue(sid: str):
        memory.store.require_langmem(sid)
        result,_=memory.searcher(memory.store.config('langmem')).prepare(sid)
        return result

    @app.get('/api/memory/langmem/export')
    def memory_export():
        from personal_workbench.memory.backup import export_store
        from fastapi.responses import Response
        # SQLite backup gives one committed snapshot while the app is running.
        content=export_store(memory.store.stores['langmem'])
        return Response(content,media_type='application/zip',headers={'Content-Disposition':'attachment; filename="langmem-memory-backup.zip"','Cache-Control':'no-store'})

    @app.get('/api/memory/spaces/{sid}/items')
    def memory_items(sid: str, q: str = Query(default='', max_length=200), page: int = Query(default=1, ge=1, le=100000), category: str = '', origin: str = '', scope_kind: str = 'personal', scope_id: str = 'personal', memory_type: str = 'fact', status: str = 'active'):
        return memory.store.list(sid, q, page, category, origin, scope=(scope_kind,scope_id), memory_type=memory_type, status=status)

    from personal_workbench.memory.config import ProfileFieldInput

    @app.post('/api/memory/spaces/{sid}/profile/fields')
    def memory_profile_field(sid: str, body: ProfileFieldInput):
        return memory.store.create_profile_field(sid, body)

    @app.delete('/api/memory/spaces/{sid}/profile/fields/{key}')
    def delete_memory_profile_field(sid: str, key: str):
        with jobs.lock:
            if jobs.active:raise ValueError('请等待当前对话任务结束后再删除档案维度。')
            fields={field['key']:field for field in memory.store.profile(sid)['fields']}
            field=fields.get(key)
            if not field:raise ValueError('档案维度不存在或已删除。')
            counts={}
            if field['memory']:
                counts=memory.lifecycle.delete_memory_only(sid,field['memory']['id']).get('counts',{})
            result=memory.store.delete_profile_field(sid,key)
            return {**result,'counts':counts}

    from personal_workbench.memory.categories import FactCategories, CategoryEdit

    @app.get('/api/memory/spaces/{sid}/categories/{category}')
    def memory_category(sid: str, category: str):
        return FactCategories(memory).read(sid, category)

    @app.put('/api/memory/spaces/{sid}/categories/{category}')
    def save_memory_category(sid: str, category: str, body: CategoryEdit):
        with jobs.lock:
            if jobs.active:raise ValueError('请等待当前对话任务结束后再保存记忆。')
            return FactCategories(memory).save(sid, category, body)

    @app.post('/api/memory/spaces/{sid}/categories/{category}/preview')
    def preview_memory_category(sid: str, category: str, body: CategoryEdit):
        return FactCategories(memory).preview(sid, category, body)

    @app.get('/api/memory/spaces/{sid}/profile')
    def memory_profile(sid: str):
        return memory.store.profile(sid)

    @app.get('/api/memory/spaces/{sid}/reviews')
    def memory_reviews(sid: str, scope_kind: str = 'personal', scope_id: str = 'personal', page: int = Query(default=1, ge=1, le=100000)):
        return memory.store.reviews(sid, (scope_kind,scope_id), page)

    @app.post('/api/memory/reviews/{rid}/resolve')
    def resolve_memory_review(rid: str, body: ReviewInput):
        return memory.store.resolve_review(rid, body)

    @app.put('/api/memory/items/{mid}/state')
    def memory_state(mid: str, body: MemoryStateInput):
        return memory.store.change_state(mid, body)

    @app.get('/api/memory/spaces/{sid}/manifests')
    def memory_manifests(sid: str, page: int = Query(default=1, ge=1, le=100000)):
        return memory.store.manifests(sid, page)

    @app.get('/api/memory/items/{mid}/history')
    def memory_history(mid: str, page: int = Query(default=1, ge=1, le=100000)):
        return memory.store.history(mid, page)

    @app.get('/api/memory/spaces/{sid}/runs')
    def memory_runs(sid: str, page: int = Query(default=1, ge=1, le=100000)):
        return memory.store.runs(sid, page)

    @app.post('/api/memory/spaces/{sid}/recall')
    def memory_recall(sid: str, body: RecallInput):
        return memory.preview(sid, body.query, (body.scope_kind,body.scope_id))

    @app.post('/api/memory/spaces/{sid}/items', status_code=201)
    def add_memory(sid: str, body: MemoryInput):
        return memory.store.create(sid, body)

    @app.put('/api/memory/items/{mid}')
    def edit_memory(mid: str, body: MemoryEdit):
        return memory.mutate(mid, body)

    @app.delete('/api/memory/items/{mid}')
    def delete_memory(mid: str):
        with jobs.lock:
            if jobs.active:raise ValueError('请等待当前对话任务结束后再删除记忆。')
            memory.mutate(mid)
        return {'deleted':True}

    @app.put("/api/settings/language")
    def save_language(body: LanguageInput):
        return preferences.save_language(body.language)

    @app.put("/api/settings/web-tools")
    def save_web_tools(body: WebToolsConfig):
        with jobs.lock:
            if jobs.active:
                raise ValueError("请等待当前对话任务结束后再保存联网设置。")
            return preferences.save_web_tools(body)

    @app.put('/api/settings/decision-tools')
    def save_decision_tools(body: DecisionToolsConfig):
        with jobs.lock:
            if jobs.active:
                raise ValueError('请等待当前对话任务结束后再保存 Jev 设置。')
            return preferences.save_decision_tools(body)

    @app.post('/api/settings/decision-tools/probe')
    def probe_decision_tool(body: DecisionToolsConfig):
        from personal_workbench.decision_tools import probe_decision, JevRequestError
        try:
            return probe_decision(preferences.resolve_decision_tools(body))
        except JevRequestError as exc:
            raise ValueError(str(exc)) from None

    @app.put("/api/settings/models")
    def save_models(body: ModelSettingsInput):
        nonlocal settings
        with jobs.lock, knowledge.lock:
            if jobs.active or any(k["status"] == "indexing" for k in knowledge.list()):
                raise ValueError("请等待当前对话和索引任务结束后再保存模型设置。")
            result = preferences.save_models(body)
            settings = preferences.runtime()
            jobs.settings = knowledge.settings = settings
            return result

    @app.post("/api/settings/probe")
    def probe_model(body: ModelProfile):
        with preferences.lock:
            profile = preferences.resolve(body)
        try:
            if profile.kind == "embedding":
                vector = knowledge.embedding(profile.embedding()).get_query_embedding("Connection test")
                return {"ok": True, "dimension": len(vector)}
            from dataclasses import replace
            from langchain_core.messages import HumanMessage
            from langchain_core.tools import tool
            from personal_workbench.models import create_model

            @tool
            def connection_check(value: str) -> dict:
                """Return the test value. No side effects."""
                return {"value": value}

            runtime = replace(settings, model=profile.model, provider=profile.provider,
                              api_key=profile.api_key, base_url=profile.base_url,
                              timeout=profile.timeout, max_tokens=profile.max_tokens)
            result = create_model(runtime, [connection_check]).invoke([
                HumanMessage(content="Call connection_check with value 'ok'. Do not answer in text.")])
            return {"ok": True, "tool_calling": any(c["name"] == "connection_check" for c in result.tool_calls)}
        except Exception as exc:
            raise ValueError(f"连接测试失败（{type(exc).__name__}）。请检查模型 ID、密钥、地址和网络。") from None

    @app.get("/api/knowledge/engine")
    def rag_engine():
        import importlib.util
        return {**knowledge.engine().model_dump(), "reranker_available": bool(importlib.util.find_spec("sentence_transformers"))}

    @app.put("/api/knowledge/engine")
    def save_rag_engine(body: EngineConfig):
        return knowledge.save_engine(body)

    @app.get("/api/knowledge/bases")
    def knowledge_bases():
        return knowledge.list()

    @app.post("/api/knowledge/bases", status_code=201)
    def create_knowledge(body: KnowledgeInput):
        return knowledge.create(body)

    @app.get("/api/knowledge/bases/{kid}")
    def get_knowledge(kid: str):
        return knowledge.get(kid)

    @app.put("/api/knowledge/bases/{kid}")
    def update_knowledge(kid: str, body: KnowledgeInput):
        return knowledge.update(kid, body)

    @app.delete("/api/knowledge/bases/{kid}")
    def remove_knowledge(kid: str):
        with jobs.lock:
            if jobs.active:
                raise ValueError("请等待当前对话任务完成或停止后再删除知识库。")
            knowledge.delete(kid)
        return {"deleted": True, "historical_citations_preserved": True}

    @app.post("/api/knowledge/bases/{kid}/probe")
    def probe_embedding(kid: str):
        return knowledge.probe(kid)

    @app.post("/api/knowledge/bases/{kid}/index", status_code=202)
    def index_knowledge(kid: str):
        return knowledge.rebuild(kid)

    @app.get("/api/knowledge/bases/{kid}/documents")
    def knowledge_documents(kid: str):
        knowledge.row(kid)
        return knowledge.library(kid).documents()

    @app.post("/api/knowledge/bases/{kid}/documents")
    async def knowledge_upload(kid: str, file: UploadFile):
        try:
            data = await file.read(UPLOAD_LIMIT + 1)
            return await asyncio.to_thread(knowledge.ingest, kid, file.filename or "", data)
        finally:
            await file.close()

    @app.delete("/api/knowledge/bases/{kid}/documents/{sid}")
    def remove_knowledge_document(kid: str, sid: str):
        knowledge.remove_document(kid, sid)
        return {"removed": True}

    @app.get("/api/knowledge/pageindex")
    def pageindex_info():
        return {"model": settings.model, "configured": configured(settings),
                "modes": ["flash", "standard"], "formats": ["pdf"]}

    @app.get("/api/knowledge/bases/{kid}/outline")
    def knowledge_outline(kid: str, source_id: str, offset: int = 0):
        from personal_workbench.pageindex_engine import outline
        return outline(knowledge, kid, source_id, offset)

    @app.get("/api/knowledge/bases/{kid}/search")
    def knowledge_search(kid: str, q: str):
        return knowledge.search(kid, q)

    @app.get("/api/documents")
    def documents():
        return library.documents()

    @app.post("/api/documents")
    async def upload(file: UploadFile):
        try:
            data = await file.read(UPLOAD_LIMIT + 1)
            return await asyncio.to_thread(library.ingest, file.filename or "", data)
        finally:
            await file.close()

    @app.post("/api/documents/sync")
    def sync():
        return library.sync_local()

    @app.delete("/api/documents/{sid}")
    def remove(sid: str):
        library.remove(sid)
        return {"removed": True}

    @app.get("/api/documents/{sid}/chunks")
    def chunks(sid: str):
        return library.chunks(sid)

    @app.get("/api/sources/{cid}")
    def source(cid: str):
        try:
            return library.source(cid)
        except ValueError:
            try:
                return knowledge.source(cid)
            except ValueError:
                return WebSources(settings).source(cid)

    @app.get("/api/search")
    def search(q: str):
        return library.search(q)

    @app.get("/api/sessions")
    def sessions(archived: bool = False, project_id: str | None = None, unassigned: bool = False,
                 pinned: bool | None = None, q: str = Query(default="", max_length=120),
                 offset: int = Query(default=0, ge=0), limit: int = Query(default=30, ge=1, le=100),
                 paged: bool = False):
        with open_service(settings, library=library, read_only=True, knowledge=knowledge) as service:
            result = projects.enrich_sessions(
                jobs.session_summaries(service.sessions()), archived=archived,
                project_id=project_id, unassigned=unassigned, pinned=pinned, query=q, offset=offset,
                limit=limit if paged else 10000,
            )
            return result if paged else result["items"]

    @app.patch("/api/sessions/{tid}")
    def update_session(tid: str, body: ConversationUpdate):
        patch = body.model_dump(exclude_unset=True)
        if not patch:
            raise ValueError("没有需要更新的会话字段。")
        if "project_id" in patch and jobs.active_for_thread(tid):
            raise ValueError("请等待当前对话完成后再移动到项目。")
        return projects.update_conversation(tid, patch)

    @app.post("/api/sessions/{tid}/archive")
    def archive_session(tid: str):
        if jobs.active_for_thread(tid):
            raise ValueError("请等待当前对话完成后再归档。")
        return projects.archive_conversation(tid, True)

    @app.post("/api/sessions/{tid}/restore")
    def restore_session(tid: str):
        return projects.archive_conversation(tid, False)

    @app.delete("/api/sessions/{tid}")
    def delete_session(tid: str, delete_artifacts: bool = False):
        return conversation_deletion.delete(tid, delete_artifacts)

    @app.get("/api/sessions/{tid}")
    def session(tid: str):
        result = jobs.inspect(tid)
        active = jobs.active_for_thread(tid)
        if active:
            result["status"] = active["status"]
        return result

    @app.get("/api/projects")
    def list_projects(archived: bool = False):
        return projects.list(archived)

    @app.post("/api/projects")
    def create_project(body: ProjectDefinition):
        return projects.create(body.model_dump())

    @app.get("/api/projects/{pid}")
    def get_project(pid: str):
        return projects.get(pid, include_archived=True)

    @app.patch("/api/projects/{pid}")
    def update_project(pid: str, body: ProjectPatch):
        patch = body.model_dump(exclude_unset=True)
        if not patch:
            raise ValueError("没有需要更新的项目字段。")
        return projects.update(pid, patch)

    @app.post("/api/projects/{pid}/archive")
    def archive_project(pid: str):
        return projects.archive(pid, True)

    @app.post("/api/projects/{pid}/restore")
    def restore_project(pid: str):
        return projects.archive(pid, False)

    @app.delete("/api/projects/{pid}")
    def delete_project(pid: str):
        return projects.delete(pid)

    @app.put("/api/sessions/{tid}/working-memory/task")
    def edit_working_task(tid: str, body: TaskEdit):
        with jobs.lock:
            if jobs.active: raise ValueError("请等待当前对话完成后再修改任务状态。")
            with open_service(settings, library=library, knowledge=knowledge) as service:
                task=service.edit_task(tid, body)
                binding=json.loads(service.session(tid)['skill_snapshot'] or '{}').get('memory') or {}
            if body.status=='completed' and binding.get('engine')=='langmem':
                selection=EpisodeSessionInput(space_id=binding['space_id'],scope_kind=binding.get('scope_kind','personal'),scope_id=binding.get('scope_id','personal'))
                capture_episode(tid,selection,completion=True,already_locked=True)
            return task

    @app.post("/api/sessions/{tid}/working-memory/rebuild")
    def rebuild_working_summary(tid: str, body: RebuildInput):
        with jobs.lock:
            if jobs.active: raise ValueError("请等待当前对话完成后再重建摘要。")
            with open_service(settings, read_only=True, library=library, knowledge=knowledge) as service:
                profile_id = service.session(tid)["model_profile_id"]
            runtime = preferences.runtime(profile_id)
            with open_service(runtime, jobs.model_override, library=library, knowledge=knowledge) as service:
                try:
                    return service.rebuild_summary(tid, body.version)
                except ValueError:
                    raise
                except Exception:
                    raise ValueError("摘要重建失败，原摘要和聊天记录已保留，请检查模型设置后重试。") from None

    @app.put("/api/sessions/{tid}/working-memory/exclusion")
    def exclude_working_turn(tid: str, body: ExclusionInput):
        with jobs.lock:
            if jobs.active: raise ValueError("请等待当前对话完成后再调整来源。")
            with open_service(settings, library=library, knowledge=knowledge) as service:
                return service.exclude_working_turn(tid, body)

    @app.get("/api/sessions/{tid}/working-memory/sources")
    def working_sources(tid: str, page: int = Query(default=1, ge=1), kind: Literal['summary','task','all'] = 'summary'):
        with open_service(settings, read_only=True, library=library, knowledge=knowledge) as service:
            state = service.status(tid)["state"]
        ids = set(state.get("running_summary" if kind == "summary" else "task_state", {}).get("source_ids", []))
        rows = [ContextEngine.source(m) for m in state.get("messages", []) if kind == "all" or m.id in ids]
        return {"items":rows[(page-1)*10:page*10], "page":page, "total":len(rows), "excluded_turn_ids":state.get("working_excluded_turns", [])}

    @app.get("/api/sessions/{tid}/working-memory/audit")
    def working_context_audit(tid: str):
        with open_service(settings, read_only=True, library=library, knowledge=knowledge) as service:
            current = service.status(tid)
            return service.context_audit(tid, current["state"])

    @app.post("/api/jobs", status_code=202)
    def ask(body: Ask):
        if not body.text.strip():
            raise ValueError("请先输入问题。")
        if body.project_id and not body.thread_id:
            projects.get(body.project_id)
        return jobs.start(body.text, body.thread_id, kb_id=body.kb_id, model_profile_id=body.model_profile_id, kb_ids=body.kb_ids, capability_id=body.capability_id, capability_version=body.capability_version, skill_refs=[ref.model_dump() for ref in body.skill_refs], connector_tool_ids=body.connector_tool_ids, memory=body.memory.model_dump() if body.memory else None, project_id=body.project_id, attachment_ids=body.attachment_ids)

    @app.get("/api/jobs")
    def list_jobs():
        return jobs.list()

    @app.get("/api/jobs/{jid}")
    def get_job(jid: str):
        return jobs.get(jid)

    @app.get("/api/jobs/{jid}/trace")
    def get_job_trace(jid: str):
        return jobs.trace(jid)

    @app.get("/api/evaluations/suites")
    def evaluation_suites():
        return evaluations.suite_list()

    @app.post("/api/evaluations/reports")
    def evaluate_job(body: EvaluateRequest):
        return evaluations.evaluate(body)

    @app.get("/api/evaluations/reports")
    def evaluation_reports(limit: int = Query(default=50,ge=1,le=200)):
        return evaluations.list(limit)

    @app.post("/api/evaluations/compare")
    def compare_evaluations(body: CompareRequest):
        return evaluations.compare(body)

    @app.get("/api/evaluations/reports/{report_id}")
    def evaluation_report(report_id: str):
        return evaluations.get(report_id)

    @app.post("/api/jobs/{jid}/cancel")
    def cancel(jid: str):
        jobs.cancel(jid)
        return {"status": "stopping"}

    @app.post("/api/sessions/{tid}/resume", status_code=202)
    def resume(tid: str, body: Resume):
        return jobs.start(thread_id=tid, resume=True, decision=body.decision)

    @app.post("/api/sessions/{tid}/abandon")
    def abandon(tid: str):
        return jobs.abandon(tid)

    @app.post("/api/sessions/{tid}/revise", status_code=202)
    def revise(tid: str, body: Ask):
        if body.thread_id is not None and body.thread_id != tid:
            raise ValueError("请求中的会话 ID 与地址不一致。")
        return jobs.revise(
            tid, body.text, kb_id=body.kb_id, model_profile_id=body.model_profile_id,
            kb_ids=body.kb_ids, skill_refs=[ref.model_dump() for ref in body.skill_refs],
            connector_tool_ids=body.connector_tool_ids,
            memory=body.memory.model_dump() if body.memory else None,
            attachment_ids=body.attachment_ids,
        )

    @app.post("/api/chat-attachments", status_code=201)
    async def upload_chat_attachment(file: UploadFile):
        data = await file.read(MAX_IMAGE_BYTES + 1)
        return attachments.save(file.filename, data, file.content_type)

    @app.get("/api/chat-attachments/{attachment_id}")
    def get_chat_attachment(attachment_id: str):
        row = attachments.row(attachment_id)
        return FileResponse(
            attachments.path(attachment_id), media_type=row["mime_type"],
            filename=row["filename"], content_disposition_type="inline",
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.delete("/api/chat-attachments/{attachment_id}", status_code=204)
    def delete_chat_attachment(attachment_id: str):
        attachments.delete_staged(attachment_id)

    @app.post("/api/sessions/{tid}/answers/{message_id}/regenerate", status_code=202)
    def regenerate_answer(tid: str, message_id: str):
        return jobs.regenerate(tid, message_id)

    @app.delete("/api/sessions/{tid}/answers/{message_id}")
    def delete_answer(tid: str, message_id: str):
        return jobs.delete_answer(tid, message_id)

    @app.get("/api/jobs/{jid}/events")
    async def events(jid: str, request: Request, after: int = 0):
        jobs.get(jid)
        try:
            after = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "无效事件游标")

        async def generate():
            cursor = after
            while not await request.is_disconnected():
                rows = jobs.events(jid, cursor)
                for row in rows:
                    cursor = row["id"]
                    yield f"id: {cursor}\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
                if jobs.get(jid)["status"] not in {"queued", "running", "stopping"}:
                    # 包括服务重启时丢失末尾事件的情况。
                    yield 'event: closed\ndata: {}\n\n'
                    return
                yield ": keepalive\n\n"
                await asyncio.sleep(0.4)

        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/notes")
    def notes():
        return library.notes()

    @app.post("/api/notes")
    def save_note(body: Note):
        return library.save_user_note(body.title, body.content, body.thread_id)

    @app.put("/api/notes/{nid}")
    def update_note(nid: str, body: Note):
        return library.save_user_note(body.title, body.content, body.thread_id, nid)

    @app.get("/api/sessions/{tid}/artifacts")
    def artifacts(tid: str):
        with open_service(settings, read_only=True) as service:
            service.session(tid)
        root = settings.outputs_dir / tid
        return Workspace(root, root).list_outputs()

    @app.get("/api/sessions/{tid}/artifacts/{name}")
    def artifact(tid: str, name: str):
        with open_service(settings, read_only=True) as service:
            service.session(tid)
        return {"title": name, "content": read_bytes(settings.outputs_dir / tid, name, ARTIFACT_SUFFIXES, MAX_ARTIFACT_BYTES).decode("utf-8")}

    dist = settings.project_dir / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/")
    def index():
        if not (dist / "index.html").is_file():
            return JSONResponse({"detail": "请先在 frontend 运行 npm ci 和 npm run build。"}, status_code=503)
        return FileResponse(dist / "index.html")

    return app


def main(argv=None):
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser(description="启动本地个人工作台")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    print(f"个人工作台：http://127.0.0.1:{args.port}  （Ctrl+C 停止）")
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")
