"""当前助手适配器；所有资料范围、模型与 LangGraph 事件细节留在能力内部。"""

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path

from personal_workbench.app_settings import configured
from personal_workbench.assistant_service import open_service, session_kb_ids
from personal_workbench.capabilities import DEFAULT_CAPABILITY, DEFAULT_VERSION, PreparedRun
from personal_workbench.file_tools import build_file_tools, TOOL_INFO
from personal_workbench.skill_store import SkillStore
from personal_workbench.skill_runtime import resolve_skills, automatic_skill_refs
from personal_workbench.observability import capability_span_id, span_id
from personal_workbench.context_window_view import context_window_view

# 仅保存确定的非密钥配置；认证始终从本地模型配置读取。
RUNTIME_FIELDS = (
    "provider", "model", "base_url", "max_tokens", "timeout",
    "max_model_calls", "max_tool_calls", "model_context_window",
    "context_window", "context_compaction_trigger", "context_compaction_target",
)


def dedupe_sources(sources):
    rows, seen = [], set()
    for source in sources or []:
        key = (source.get("kind"), source.get("url") or source.get("path"),
               source.get("source_id"), source.get("page"), source.get("start"), source.get("end"))
        if key in seen:
            continue
        seen.add(key)
        rows.append(source)
    return rows


class AssistantCapability:
    id, version = DEFAULT_CAPABILITY, DEFAULT_VERSION
    partner_definition = None
    profile_store = None

    def __init__(self, settings, library, knowledge=None, model_override=None):
        self.settings, self.library, self.knowledge = settings, library, knowledge
        self.model_override = model_override
        self.app_settings = None
        self.connectors = None
        from personal_workbench.chat_attachments import ChatAttachmentStore
        self.attachments = ChatAttachmentStore(settings)
        self.skills = SkillStore(settings)
        from personal_workbench.bundled_skills import sync_bundled_skills
        sync_bundled_skills(self.skills, settings.project_dir)
        from personal_workbench.external_skills import sync_external_skills
        sync_external_skills(self.skills)
        from personal_workbench.memory import MemoryService
        self.memory = MemoryService(settings)
        from personal_workbench.memory.learning import LearningQueue
        self.learning = LearningQueue(self.memory)

    def describe(self):
        if self.profile_store:
            return self.profile_store.get()
        return {"id": self.id, "version": self.version, "name": "工作台助手",
                "description": "理解资料、回答问题，整理并保存学习笔记。",
                "source": "builtin", "enabled": True, "dependencies": ["chat_model"],
                "tool_ids": [*TOOL_INFO, "read_skill_resource"]}

    def service(self, runtime=None, **kwargs):
        return open_service(runtime or self.settings, self.model_override,
                            library=self.library, knowledge=self.knowledge, connectors=self.connectors,
                            attachments=self.attachments, **kwargs)

    def inspect(self, thread_id):
        with self.service(read_only=True) as service:
            result = service.status(thread_id)
            audit = service.context_audit(thread_id, result["state"])
        state = result.pop("state")
        result["messages"], runs = [], []
        current = None
        deleted_answer_ids = set(state.get("deleted_answer_ids", []))
        for message in state.get("messages", []):
            if message.type == "human":
                current = {"run_id":message.additional_kwargs.get("run_id"), "skill_refs":message.additional_kwargs.get("skill_refs",[]), "resources":[], "tools":[]}
                runs.append(current)
            if message.type == "tool" and getattr(message,"name",None) in {"read_skill_resource", "skill_view"} and current:
                try:
                    data=json.loads(message.content)
                    if isinstance(data,dict) and data.get("skill_read"): current["resources"].append(data["skill_read"])
                except (ValueError,TypeError): pass
            if message.type == 'tool' and str(getattr(message,'name','')).startswith('mcp_') and current:
                try:
                    data=json.loads(message.content)
                    label=' / '.join(str(data.get('mcp',{}).get(k,'')) for k in ('connector','tool')).strip(' / ')
                    current['tools'].append({'name':message.name,'label':label or message.name,'status':message.status,'result':data})
                except (ValueError,TypeError): pass
            if (message.type in {"human","ai"} and not getattr(message,"tool_calls",None)
                    and message.id not in deleted_answer_ids):
                content = message.additional_kwargs.get('display_content', message.text) if message.type == 'human' else message.text
                result["messages"].append({"id":message.id,"role":message.type,"content":content,
                                           "run_id":current["run_id"] if current else None,
                                           "skill_refs":current["skill_refs"] if current else [],
                                           "attachments":message.additional_kwargs.get("attachments", []) if message.type == "human" else []})
        saved_snapshot=json.loads(result.pop("skill_snapshot") or '{}')
        result["skill_refs"]=saved_snapshot.get("skill_refs",[])
        result["partner"]=saved_snapshot.get("partner")
        result["assistant_profile"]=saved_snapshot.get("assistant_profile")
        if result['partner'] and self.partner_definition:
            # Enrich the public view of pre-numbered sessions, never rewrite
            # the saved execution snapshot used by resume.
            result['partner'] = {**result['partner'], 'version_number':self.partner_definition['version_number']}
        result["effective_tool_ids"]=saved_snapshot.get("tool_ids",[])
        result["skill_runs"]=runs
        result["connector_runs"]=[{"run_id":r["run_id"],"tools":r["tools"]} for r in runs]
        result["connector_tool_ids"]=[r["id"] for r in saved_snapshot.get("connector_tools",[])]
        result["memory"] = saved_snapshot.get("memory")
        result["memory_binding"] = self.memory.store.registry.binding(thread_id) or (saved_snapshot.get("memory") or {}).get("memory_profile_id")
        result["legacy_mixed_memory"] = bool(result["memory"] and result["memory"].get("engine") == "mem0" and not result["memory"].get("memory_profile_id"))
        result["working_memory"] = {key:state.get(key, {}) for key in (
            "task_state", "running_summary", "context_compression",
            "context_budget", "context_usage", "request_summary")}
        result["working_memory"]["context_layers"] = state.get("context_budget", {}).get("layers", [])
        result["working_memory"]["error"] = state.get("context_error", "")
        result["working_memory"]["notice"] = state.get("context_notice", "")
        result["working_memory"]["diagnostic"] = state.get("context_diagnostic", {})
        result["working_memory"]["engine"] = state.get("context_engine", "shared-context-v1")
        result["working_memory"]["tool_results"] = state.get("tool_result_refs", [])
        result["working_memory"]["audit"] = audit
        profile = None
        if self.app_settings:
            try:
                profile = self.app_settings.profile(result.get("model_profile_id"))
            except ValueError:
                # A deleted model profile does not make an existing session
                # unreadable; its persisted budget remains authoritative.
                pass
        result["context_window"] = context_window_view(
            state.get("context_budget", {}),
            model=getattr(profile, "model", self.settings.model),
            context_window=getattr(profile, "context_window", self.settings.context_window),
            model_context_window=getattr(
                profile, "model_context_window", self.settings.model_context_window),
            max_tokens=getattr(profile, "max_tokens", self.settings.max_tokens),
            compaction_trigger=getattr(
                profile, "context_compaction_trigger", self.settings.context_compaction_trigger),
            compaction_target=getattr(
                profile, "context_compaction_target", self.settings.context_compaction_target),
        )
        result["sources"] = state.get("sources", [])
        result["usage"] = {key: state.get(key) for key in ("model_calls", "tool_calls", "usage_tokens", "usage_unknown")}
        return result

    def abandon(self, thread_id):
        with self.service(execution_scope=thread_id) as service:
            service.abandon(thread_id)
        return self.inspect(thread_id)

    def supersede(self, thread_id, run_id=None):
        """Close the stopped turn and omit it from subsequent model context."""
        with self.service(execution_scope=thread_id) as service:
            if run_id:
                service.supersede_run(thread_id, run_id)
            else:
                service.abandon(thread_id, supersede=True)
        return self.inspect(thread_id)

    def delete_answer(self, thread_id, message_id):
        with self.service(execution_scope=thread_id) as service:
            service.delete_answer(thread_id, message_id)
        return self.inspect(thread_id)

    def prepare(self, request, snapshot=None):
        request = dict(request)
        tid, resume = request["thread_id"], request["resume"]
        if not resume:
            from personal_workbench.external_skills import sync_external_skills
            sync_external_skills(self.skills)
        assistant_profile = None
        with self.service(read_only=True) as service:
            saved = service.session(tid) if request["existing"] else None
            # The built-in assistant keeps one immutable platform identity and a
            # versioned user overlay. New chats freeze the latest overlay; old
            # and legacy chats keep the profile (or lack of profile) they began with.
            if not self.partner_definition and self.profile_store:
                if resume and snapshot:
                    assistant_profile = snapshot.get('assistant_profile')
                elif saved:
                    assistant_profile = json.loads(saved['skill_snapshot'] or '{}').get('assistant_profile')
                else:
                    current = self.profile_store.get()
                    assistant_profile = {key:value for key,value in current.items()
                                         if key not in {'revisions','system_config','issues','enabled','archived'}}
                if assistant_profile and not resume:
                    bound = [{**ref, 'selection':'bound'} for ref in assistant_profile.get('skill_refs', [])]
                    if bound:
                        requested = [{key:value for key,value in ref.items() if key != 'selection'}
                                     for ref in request.get('skill_refs', [])]
                        bound_refs = [{key:value for key,value in ref.items() if key != 'selection'} for ref in bound]
                        if requested and requested != bound_refs:
                            raise ValueError('工作台助手已绑定默认技能；请修改助手配置并新建对话。')
                        request['skill_refs'] = bound
                    if not request['existing'] and request.get('model_profile_id') is None:
                        request['model_profile_id'] = assistant_profile.get('model_profile_id')
            if saved is not None:
                request["project_id"] = saved["project_id"]
                state = service.status(tid)
                if resume:
                    if not state["next"]:
                        raise ValueError("没有待恢复的任务。")
                    if bool(state["pending"]) != (request["decision"] is not None):
                        raise ValueError("覆盖确认需要明确批准或拒绝；普通恢复不传决定。")
                    request["kb_id"] = saved["kb_id"]
                    request["kb_ids"] = session_kb_ids(saved) if saved["kb_ids"] is not None else None
                    request["model_profile_id"] = saved["model_profile_id"]
                else:
                    if request["kb_ids"] is None and saved["kb_id"] != request["kb_id"]:
                        raise ValueError("请新建对话以切换知识库。")
                    revising = request.get("revises_run_id") == state.get("state", {}).get("turn_id")
                    if state["next"] and not revising:
                        raise ValueError("请先恢复当前任务，或新建对话。")
                if request["model_profile_id"] is None:
                    request["model_profile_id"] = saved["model_profile_id"]
            if resume and snapshot:
                for key in ("kb_id", "kb_ids", "model_profile_id"):
                    request[key] = snapshot[key]
                request["project_id"] = snapshot.get("project_id", request.get("project_id"))
                # 恢复必须使用原会话范围；不静默使用后来修改的范围。
                actual = session_kb_ids(saved) if saved["kb_ids"] is not None else None
                if actual != request["kb_ids"] or saved["kb_id"] != request["kb_id"]:
                    raise ValueError("会话资料范围与运行快照不一致，无法恢复。")
            profile = request["model_profile_id"]
            profile_record = self.app_settings.profile(profile) if self.app_settings else None
            runtime = self.app_settings.runtime(profile) if self.app_settings else self.settings
            if self.app_settings:
                request["model_profile_id"] = self.app_settings.profile(profile).id
            if request.get("attachment_ids") and profile_record and "image" not in profile_record.input_modalities:
                raise ValueError("所选模型未启用图片输入，请更换模型或在设置中开启。")
            if request.get("attachment_ids") and profile_record is None and self.model_override is None:
                raise ValueError("当前模型没有声明图片输入能力。")
            if resume and snapshot:
                values = snapshot["model"]
                if any(getattr(runtime, key) != values[key] for key in ("provider", "base_url")):
                    raise ValueError("模型服务地址已改变，请恢复原模型配置后继续任务。")
                runtime = replace(runtime, **{key: values.get(key, getattr(runtime, key)) for key in RUNTIME_FIELDS})
            kids = request["kb_ids"]
            request["kb_ids"] = list(dict.fromkeys(kids)) if kids is not None else None
            # 显式多库的单库值与 AssistantService 持久化行为一致。
            if request["kb_ids"] is not None:
                request["kb_id"] = request["kb_ids"][0] if len(request["kb_ids"]) == 1 else None
            scope = request["kb_ids"] if kids is not None else ([request["kb_id"]] if request["kb_id"] else [])
            if request["decision"] is not False:
                if scope and self.knowledge is None:
                    raise ValueError("知识仓库不可用。")
                for kid in scope:
                    self.knowledge.ready(kid)
                if not configured(runtime) and self.model_override is None:
                    raise ValueError("请在设置中配置模型，或选择本机模型。")
            previous_memory = json.loads(saved["skill_snapshot"] or '{}').get('memory') if saved else None
            registry = self.memory.store.registry
            if previous_memory and previous_memory.get('engine') == 'mem0' and not previous_memory.get('memory_profile_id'):
                raise ValueError('旧版 Mem0 混合会话保留原记录；请新建对话使用独立 Mem0 方案。')
            selection = request.get('memory')
            if selection is None and self.memory.store.stores['langmem'].global_scope and not resume:
                from personal_workbench.memory.workspace import WorkspaceMemory
                selection=WorkspaceMemory(self.memory).selection()
                if previous_memory and previous_memory.get('space_id')!=selection['space_id']:
                    raise ValueError('当前记忆空间已改变，请新建对话继续。')
            if selection is None:
                if previous_memory:
                    selection = previous_memory.get('selection') or {k:previous_memory[k] for k in ('space_id','use_memories','learn_memories')}
                else:
                    active = registry.state()['active_profile_id']
                    space = next(s for s in self.memory.store.spaces() if s['memory_profile_id'] == active)
                    selection = {'space_id':space['id']}
            with registry.guard():
                if resume and snapshot and snapshot.get('memory'):
                    memory_snapshot = {**snapshot['memory'], 'activation_epoch':registry.state()['epoch']}
                    if not memory_snapshot.get('memory_profile_id'):
                        memory_snapshot['memory_profile_id'] = 'langmem-default'
                    registry.require_active(memory_snapshot['memory_profile_id'])
                else:
                    memory_snapshot = self.memory.freeze(selection)
                old_profile = registry.binding(tid) or (previous_memory or {}).get('memory_profile_id')
                if saved and not old_profile:
                    # Legacy LangMem/CLI context is never attributed to Mem0.
                    old_profile = 'langmem-default'
                if old_profile and old_profile != memory_snapshot['memory_profile_id']:
                    raise ValueError('此会话绑定了另一个记忆方案，请明确切回原方案或新建对话。')
                registry.bind(tid, memory_snapshot)
            # A missing kb_ids field is the legacy local-library API. An
            # explicit [] from the web composer means ordinary conversation.
            workspace_spec = {"library_mode": saved["library_mode"] if resume else int(request["kb_ids"] is None and not request["kb_id"]),
                              "kb_id": request["kb_id"],
                              "kb_ids": json.dumps(request["kb_ids"]) if request["kb_ids"] is not None else None}
            workspace, _ = service.workspace_for(workspace_spec, tid, create_output=False)
            tool_ids = [tool.name for tool in build_file_tools(workspace)]
            tool_ids += ['skills_list', 'skill_view', 'terminal']
            from personal_workbench.web_tools.config import WebToolsConfig
            frozen_web = snapshot.get('web_tools', {}) if resume and snapshot else None
            web_config = self.app_settings.web_config(frozen_web) if self.app_settings else WebToolsConfig()
            tool_ids += web_config.enabled_ids()
            from personal_workbench.decision_tools.config import DecisionToolsConfig
            frozen_decisions = snapshot.get('decision_tools', {}) if resume and snapshot else None
            decision_config = self.app_settings.decision_config(frozen_decisions) if self.app_settings else DecisionToolsConfig()
            tool_ids += decision_config.enabled_ids()
            from personal_workbench.memory.hot_path import allowed_names
            tool_ids += [name for name in allowed_names(memory_snapshot) if not resume or not snapshot or name in snapshot['tool_ids']]
            effective_definition = self.partner_definition or assistant_profile
            if effective_definition:
                tool_ids = [name for name in tool_ids if name in effective_definition['tool_ids']]
            # Agent 只能在用户本轮明确提出技能创作/修改时提交待审批建议；
            # 该工具本身不写入正式技能目录，也不授予脚本或其他运行权限。
            if not self.partner_definition and re.search(
                r'(?:创建|新建|制作|编写|生成|改进|优化|修改|更新)(?:一个|这个|该|现有的)?[^，。！？\n]{0,12}(?:技能|skill)|(?:这个|该|现有)?(?:技能|skill)[^，。！？\n]{0,4}(?:优化|修改|更新)',
                request.get('text') or '', re.I):
                tool_ids.append('propose_skill')
            refs = request.get("skill_refs", [])
            frozen_refs = snapshot.get("skill_refs",[]) if resume and snapshot else None
            if not resume and not self.partner_definition and not (assistant_profile and assistant_profile.get('skill_refs')):
                refs = automatic_skill_refs(self.skills, request.get('text',''),
                                            [{**ref,'selection':ref.get('selection','manual')} for ref in refs])
            if refs or frozen_refs:
                if scope:
                    engines={self.knowledge.row(kid,include_deleted=True)["engine"] for kid in scope}
                    if "pageindex" not in engines: tool_ids=[x for x in tool_ids if x not in {"read_page","read_outline"}]
                    if "llamaindex" not in engines: tool_ids=[x for x in tool_ids if x not in {"search_files","read_file"}]
                refs, tool_ids=resolve_skills(self.skills,refs,tool_ids,bool(scope),frozen_refs)
            request["skill_refs"] = refs
            if resume and snapshot and tool_ids != snapshot["tool_ids"]:
                raise ValueError("工具范围已改变，请恢复原能力版本后继续任务。")
        connector_ids = request.get('connector_tool_ids') or []
        if self.partner_definition:
            allowed = self.partner_definition.get('connector_tool_ids', [])
            if set(connector_ids)-set(allowed): raise ValueError('伙伴未授权所选连接器工具。')
        elif assistant_profile and assistant_profile.get('connector_tool_ids'):
            allowed = assistant_profile['connector_tool_ids']
            if set(connector_ids)-set(allowed): raise ValueError('工作台助手未授权所选连接器工具。')
        connector_refs = snapshot.get('connector_tools',[]) if resume and snapshot else []
        if resume and connector_refs:
            if self.connectors is None: raise ValueError('连接器运行时不可用。')
            # Reject remains possible even if a connector has since disconnected.
            if request['decision'] is not False:
                for ref in connector_refs: self.connectors.validate(ref)
        elif connector_ids:
            if self.connectors is None: raise ValueError('连接器运行时不可用。')
            connector_refs = self.connectors.freeze(connector_ids)
        if request.get('skill_refs'):
            for skill_ref in request['skill_refs']:
                meta=self.skills.revision(skill_ref['id'],skill_ref['revision'],verify=False)
                for dependency in meta.get('connector_dependencies',[]):
                    matching=[]
                    if self.connectors:
                        for connector_ref in connector_refs:
                            config=self.connectors.store.raw(connector_ref['connector_id'])
                            if config.get('package_id')==dependency['package_id']: matching.append(connector_ref)
                    required=set(dependency.get('tools',[]))
                    if not matching or required-{ref['name'] for ref in matching}:
                        raise ValueError(f'技能“{skill_ref["name"]}”需要连接器 {dependency.get("name") or dependency["package_id"]}；请安装、连接并选择所需工具。')
        if profile_record:
            from personal_workbench.pricing import resolve_pricing
            pricing_snapshot = resolve_pricing(profile_record)
        else:
            pricing_snapshot = None
        frozen = {**snapshot, 'memory':memory_snapshot} if resume and snapshot else {
            "capability_id": self.id, "capability_version": self.version,
            "model_profile_id": request["model_profile_id"],
            "model": {key: getattr(runtime, key) for key in RUNTIME_FIELDS},
            "input_modalities": profile_record.input_modalities if profile_record else (["text", "image"] if self.model_override else ["text"]),
            "pricing": pricing_snapshot,
            "kb_id": request["kb_id"], "kb_ids": request["kb_ids"],
            "library_mode": workspace_spec["library_mode"], "tool_ids": tool_ids,
            "skill_refs":request["skill_refs"], "run_id":request.get("run_id"),
            "partner":self.partner_definition, "assistant_profile":assistant_profile,
            "connector_tools":connector_refs, "web_tools":web_config.public(),
            "decision_tools":decision_config.public(),
            "memory":memory_snapshot, "project_id":request.get("project_id"),
        }
        return PreparedRun(request, frozen, runtime)

    def execute(self, prepared, stop, emit):
        frozen = prepared.snapshot.get('memory') or {}
        if prepared.snapshot.get('skill_refs'):
            emit('progress',label='正在核验技能运行环境')
            emit('progress',label='已加载本轮技能方法')
        req = prepared.request
        rid = prepared.snapshot['run_id']
        agent_span = span_id('agent',rid,'primary')
        actor = self.partner_definition or prepared.snapshot.get('assistant_profile') or {}
        emit('trace_span',span_id=agent_span,parent_span_id=capability_span_id(rid),span_type='agent',
             name=actor.get('name','工作台助手'),status='running',
             attributes={'role':'primary'})
        episode_job = None
        if frozen.get('engine') == 'langmem':
            if req['resume']:
                with self.memory.store.registry.guard(), self.memory.episodes.store.connect() as db:
                    row = db.execute('SELECT id,state,frozen FROM episode_runs WHERE space_id=? AND thread_id=? AND run_id=?', (frozen['space_id'],req['thread_id'],prepared.snapshot['run_id'])).fetchone()
                    episode_job = row['id'] if row and row['state']=='collecting' else None
                    if episode_job:
                        old=json.loads(row['frozen']);old['activation_epoch']=frozen['activation_epoch']
                        db.execute('UPDATE episode_runs SET frozen=? WHERE id=?',(json.dumps(old),episode_job))
            else:
                episode_job = self.memory.episodes.begin(frozen,req['text'],req['thread_id'],prepared.snapshot['run_id'])
        try:
            result = self._execute(prepared,stop,emit,episode_job)
        except Exception:
            self.memory.episodes.finish(episode_job,'interrupted') if episode_job else None
            emit('trace_span',span_id=agent_span,parent_span_id=capability_span_id(rid),span_type='agent',
                 name=actor.get('name','工作台助手'),status='failed')
            raise
        if episode_job:self.memory.episodes.finish(episode_job,result['status'])
        emit('trace_span',span_id=agent_span,parent_span_id=capability_span_id(rid),span_type='agent',
             name=actor.get('name','工作台助手'),status=result['status'],
             usage=result.get('usage') or {})
        return result

    def _execute(self, prepared, stop, emit, episode_job=None):
        req = prepared.request
        rid = prepared.snapshot['run_id']
        agent_span = span_id('agent',rid,'primary')
        retrieval_tools = {'list_files','search_files','read_file','read_outline','read_page',
                           'skills_list','skill_view','read_skill_resource','read_tool_result',
                           'search_conversation','read_conversation_message',
                           'search_memory','web_search','web_fetch','paper_search'}

        def tool_trace_id(call_id): return span_id('tool',rid,call_id)

        def artifact(name, parent):
            candidate = Path(name)
            if candidate.name != name or candidate.suffix not in {'.md','.txt','.html','.css','.js','.json'}:
                return None
            path = self.settings.outputs_dir / req['thread_id'] / name
            if path.is_symlink() or not path.is_file(): return None
            data = path.read_bytes()
            item = {'kind':'file','name':name,'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)}
            emit('trace_span',span_id=span_id('artifact',rid,name),parent_span_id=parent,span_type='artifact',
                 name=name,status='completed',attributes=item)
            return item

        def event(node, update):
            if node == "session":
                for ref in prepared.snapshot.get("skill_refs",[]):
                    emit("skill_loaded", name=ref["name"], revision=ref["revision"], run_id=prepared.snapshot.get("run_id"))
            if node == "context_progress":
                emit("progress", label=update.get("label") or "正在准备会话上下文")
                return
            labels = {"context": "正在准备会话上下文", "maintain": "正在整理当前任务", "model": "已收到模型回复", "tools": "已完成工具调用", "approve": "已处理保存决定", "save": "已处理文件保存"}
            if node == "__interrupt__":
                emit("progress", label="等待你确认操作内容")
            elif node != "session":
                emit("progress", label=labels.get(node, node))
            if isinstance(update, dict):
                if update.get("context_notice"):
                    emit("progress", label=update["context_notice"])
                if episode_job and update.get('messages'):
                    self.memory.episodes.observe(episode_job,update['messages'])
                if update.get("skill_reads"):
                    emit("skill_resource", **update["skill_reads"][-1])
                for message in update.get("messages", []):
                    if node == 'model' and message.type == 'ai':
                        usage = getattr(message,'usage_metadata',None) or {}
                        attempts = int((getattr(message,'additional_kwargs',None) or {}).get('_workbench_model_attempts') or 1)
                        input_details = usage.get('input_token_details') or {}
                        cached = input_details.get('cache_read') or input_details.get('cached_tokens') or 0
                        cache_write = (input_details.get('cache_creation') or input_details.get('cache_write')
                                       or input_details.get('cache_write_tokens') or 0)
                        model = prepared.snapshot.get('model',{})
                        emit('trace_span',span_id=span_id('model',rid,message.id or update.get('model_calls',0)),
                             parent_span_id=agent_span,span_type='model',name=model.get('model','语言模型'),status='completed',
                             attributes={'provider':model.get('provider'),'model':model.get('model'),
                                         'context_engine':update.get('context_engine','shared-context-v1'),
                                         'context_layer_version':update.get('context_budget',{}).get('layer_version'),
                                         'context_estimated_input':update.get('context_budget',{}).get('estimated_input'),
                                         'context_input_capacity':update.get('context_budget',{}).get('input_capacity'),
                                         'context_compressed_messages':update.get('context_budget',{}).get('compression',{}).get('compressed_messages'),
                                         'externalized_results':len(update.get('tool_result_refs',[]))},
                             usage={'model_calls':attempts,'input_tokens':usage.get('input_tokens',0),
                                    'output_tokens':usage.get('output_tokens',0),
                                    'cached_input_tokens':cached,
                                    'cache_write_tokens':cache_write,
                                    'usage_tokens':usage.get('total_tokens',0),'usage_unknown':not bool(usage)})
                    for call in getattr(message, "tool_calls", []):
                        ref=next((r for r in prepared.snapshot.get('connector_tools',[]) if r['id']==call['name']),None)
                        emit("progress", label=(ref['connector_name']+' / '+ref['name']) if ref else TOOL_INFO.get(call["name"], ("处理工具请求",))[0])
                        emit('trace_span',span_id=tool_trace_id(call['id']),parent_span_id=agent_span,
                             span_type='retrieval' if call['name'] in retrieval_tools else 'tool',
                             name=(ref['connector_name']+' / '+ref['name']) if ref else call['name'],status='running',
                             attributes={'tool_id':call['name']})
                    if message.type == 'tool':
                        try: payload=json.loads(message.text)
                        except (ValueError,TypeError): payload={}
                        tool_name=getattr(message,'name','tool') or 'tool'
                        emit('trace_span',span_id=tool_trace_id(message.tool_call_id),parent_span_id=agent_span,
                             span_type='retrieval' if tool_name in retrieval_tools else 'tool',name=tool_name,
                             status='failed' if getattr(message,'status','')=='error' else 'completed',
                             attributes={'tool_id':tool_name,
                                         'source_count':len(payload.get('sources',[])) if isinstance(payload,dict) else 0,
                                         'result_id':payload.get('result_id') if isinstance(payload,dict) else None,
                                         'result_stored':bool(payload.get('stored')) if isinstance(payload,dict) else False,
                                         'result_bytes':payload.get('bytes') if isinstance(payload,dict) else None})
                    if message.type == "ai" and not message.tool_calls:
                        emit("message", text=message.text)

        frozen_memory = prepared.snapshot.get('memory') or {}
        queued = None
        if frozen_memory.get('engine') == 'langmem' and not req['resume']:
            queued = self.learning.enqueue(frozen_memory, req['text'], req['thread_id'], prepared.snapshot['run_id'], await_answer=True)
        native_queued = None
        if frozen_memory.get('engine')=='mem0' and self.memory.native_mem0.ready and not req['resume']:
            native_queued=self.memory.native_mem0.enqueue(frozen_memory,req['text'],req['thread_id'],prepared.snapshot['run_id'],await_answer=True)
        with self.service(prepared.runtime, stop_event=stop, execution_scope=req['thread_id']) as service:
            pending_before = service.status(req['thread_id']).get('pending',[]) if req['resume'] else []
            if req["resume"]:
                result = service.resume(req["thread_id"], req["decision"], event)
            else:
                result = service.ask(req["text"], req["thread_id"], on_event=event,
                                     library_mode=bool(prepared.snapshot.get("library_mode", False)),
                                     kb_id=req["kb_id"], model_profile_id=req["model_profile_id"], kb_ids=req["kb_ids"],
                                     skill_snapshot=prepared.snapshot, run_id=prepared.snapshot.get("run_id"),
                                     project_id=req.get("project_id"), attachments=prepared.snapshot.get("attachments", []))
            for pending in pending_before:
                emit('trace_span',span_id=span_id('approval',rid,pending.get('approval_id','legacy')),
                     parent_span_id=agent_span,span_type='approval',name=pending.get('kind','approval'),
                     status='completed' if req['decision'] else 'rejected',
                     attributes={'approval_id':pending.get('approval_id'),'decision':bool(req['decision'])})
            for pending in result.get('pending',[]):
                emit('trace_span',span_id=span_id('approval',rid,pending.get('approval_id','legacy')),
                     parent_span_id=agent_span,span_type='approval',name=pending.get('kind','approval'),
                     status='waiting_approval',attributes={'approval_id':pending.get('approval_id')})
            state = result["state"]
            if prepared.snapshot.get('skill_refs'):
                emit('progress',label='正在收集技能执行结果')
            messages = state.get("messages", [])
            # 结果必须是可持久化的公共数据，不暴露模型客户端或图内部对象。
            last_input = max((i for i, message in enumerate(messages) if message.type == "human"), default=-1)
            output = next((m.text for m in reversed(messages[last_input + 1:]) if m.type == "ai" and not m.tool_calls), "")
            memory_result = None
            if result["status"] == "completed" and last_input >= 0 and prepared.snapshot.get("memory"):
                memory_input = messages[last_input].additional_kwargs.get('display_content', messages[last_input].text)
                if frozen_memory.get('engine') == 'langmem':
                    queued = queued or self.learning.enqueue(frozen_memory,memory_input,req['thread_id'],prepared.snapshot.get('run_id') or state['turn_id'])
                    if queued.get('job_id'):
                        queue_state = self.learning.release(queued['job_id'])
                        queued['status'] = 'queued' if queue_state == 'pending' else queue_state
                    memory_result = queued
                    if queued['status'] == 'queued':
                        emit('progress',label='记忆已进入后台整理队列，可以继续提问。')
                else:
                    if native_queued and native_queued.get('operation_id'):
                        memory_result=self.memory.native_mem0.release(native_queued['operation_id'],messages=messages[last_input+1:])
                        if memory_result['status']=='pending':memory_result['status']='queued'
                    else:
                        memory_result = self.memory.learn(frozen_memory,memory_input,req['thread_id'],prepared.snapshot.get('run_id') or state['turn_id'],stop)
                        if memory_result.get('operation_id'):self.memory.native_mem0.release(memory_result['operation_id'],messages=messages[last_input+1:])
                    if memory_result['status'] != 'disabled':
                        emit('progress',label='记忆已进入后台整理队列，可以继续提问。' if memory_result['status']=='queued' else '记忆整理失败，回答已保留；请到记忆中心检查。' if memory_result['status']=='failed' else '本轮记忆整理完成')
            elif queued and queued.get('job_id') and result['status'] == 'limited':
                self.learning.release(queued['job_id'],completed=False)
            if native_queued and native_queued.get('operation_id') and result['status']=='limited':self.memory.native_mem0.release(native_queued['operation_id'],completed=False)
            artifacts=[]
            for message in messages[last_input+1:]:
                if message.type!='tool' or getattr(message,'name','') not in {'create_note','create_artifact','run_skill_script'}: continue
                try: payload=json.loads(message.text)
                except (ValueError,TypeError): continue
                if isinstance(payload,dict) and payload.get('saved'):
                    item=artifact(str(payload['saved']),tool_trace_id(message.tool_call_id))
                    if item and item not in artifacts:artifacts.append(item)
                if isinstance(payload,dict):
                    for generated in payload.get('artifacts',[]):
                        if not isinstance(generated,dict) or not generated.get('path'): continue
                        item=artifact(str(generated['path']),tool_trace_id(message.tool_call_id))
                        if item and item not in artifacts:artifacts.append(item)
            if artifacts:
                emit('progress',label=f'已保存 {len(artifacts)} 个技能产物')
            requires_artifact = any(ref.get('output_contract') == 'artifact'
                                    for ref in prepared.snapshot.get('skill_refs', []))
            if prepared.snapshot.get('skill_refs'):
                emit('progress',label='正在校验技能结果')
            final_status = result['status']
            if final_status == 'completed' and requires_artifact and not artifacts:
                final_status = 'failed'
                output = ((output + '\n\n') if output else '') + '技能要求生成文件，但本轮没有产生可交付文件。'
                emit('problem',message='技能未生成必需产物。',error_type='MissingSkillArtifact')
            if memory_result and memory_result.get('status') not in ('disabled',None):
                memory_status={'queued':'pending','pending':'pending','failed':'failed'}.get(memory_result['status'],'completed')
                emit('trace_span',span_id=span_id('memory',rid,frozen_memory.get('engine','memory')),
                     parent_span_id=agent_span,span_type='memory',name=frozen_memory.get('engine','memory'),status=memory_status,
                     attributes={'engine':frozen_memory.get('engine')})
            from personal_workbench.assistant_graph import latest_operation_receipt
            operation_receipt = latest_operation_receipt(state)
            return {"status": final_status, "thread_id": req["thread_id"], "output": output,
                    "memory_result": memory_result,
                    "sources": dedupe_sources(state.get("sources", [])),
                    "tool_results": state.get("tool_result_refs", []),
                    "operation_receipt": operation_receipt,
                    "artifacts":artifacts,
                    "usage": {key: state.get(key) for key in ("model_calls", "tool_calls", "usage_tokens", "usage_unknown")}}
