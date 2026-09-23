import { t } from './i18n';
import { type WorkingContext } from './WorkingMemoryPanel';
import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ArrowUp, BookOpen, BookMarked, Check, ChevronRight, Copy, FileText, FolderOpen, ImagePlus, Leaf, LoaderCircle, MessageSquare, Pencil, Plus, RefreshCw, Square, Trash2, X, Download, PanelRightClose, PanelRightOpen, Settings2, Sparkles, Brain } from 'lucide-react';
import './style.css';
import './answer-actions.css';
import { ApiError, api } from './api';
import { SkillCenter, type CenterTab } from './SkillCenter';
import type { ConnectorSummary } from './ConnectorCatalog';
import { partnerVersionLabel, type Partner, type PartnerDefinition } from './PartnerCatalog';
import { type Skill, type SkillRef, versionLabel } from './SkillCatalog';
import { SettingsPage, type Preferences } from './SettingsPage';
import { MemoryCenter, type MemoryOverview, type MemorySelection } from './MemoryCenter';
import { ModelPicker } from './ModelPicker';
import { ContextWindowChip, contextWindowForProfile, type ContextWindowView } from './ContextWindowChip';
import { CapabilitySuggestions } from './CapabilitySuggestions';
import { WorkflowRunCards, type WorkflowRun } from './WorkflowCatalog';
import { WorkflowToolPreview } from './WorkflowToolPreview';
import { ComposerContextMenu } from './ComposerContextMenu';
import { useLanguage, setLanguage } from './i18n';
import { KnowledgeCenter, type KnowledgeBase } from './KnowledgeCenter';
import { ReasoningPanel, type ReasoningTrace } from './ReasoningPanel';
import { ConversationSidebar } from './ConversationSidebar';
import type {Project,SessionPage,SessionSummary} from './conversations';
import chatWelcomeLogo from './assets/chat-welcome-logo.jpg';
import auroLogo from './assets/auro-logo.svg';

type Session = SessionSummary;
type Chunk = { external?:boolean; url?:string; kind?:string; retrieved_at?:string; id: string; source_id: string; title: string; text: string; version: string; page: number | null; paragraph: number; start_line: number; end_line: number; archived?: boolean; rag?: boolean; kb_name?: string };
type SavedNote = { id: string; title: string; content: string; thread_id?: string };
type Job = { id: string; thread_id: string; status: string; created?: string; error?: string; queue_reason?: string; resource_class?: string; snapshot?: { run_id?: string }; revises_run_id?: string; regenerates_run_id?:string; branched?:boolean };
type ChatAttachment = {id:string;name:string;mime_type:string;bytes:number;width:number;height:number;url:string};
type EditableTurn = { message_id?: string; run_id?: string; content: string; attachments?:ChatAttachment[] };
type ChatMessage = { id: string; role: string; content: string; attachments?:ChatAttachment[]; skill_refs?: SkillRef[]; run_id?: string; turn_state?: 'superseded' | 'revision'; revised_by_run_id?: string; revises_run_id?: string };
type Chat = Session & { reasoning_runs?:Record<string,ReasoningTrace>; context_window?: ContextWindowView; editable_turn?: EditableTurn | null; turn_revisions?: {original_run_id:string;revised_run_id:string;created:string}[]; memory_binding?:string|null; legacy_mixed_memory?:boolean; working_memory?: WorkingContext; memory?: MemorySelection & { selection?: MemorySelection }; workflow_runs?:WorkflowRun[]; connector_tool_ids?: string[]; connector_runs?:{run_id:string;tools:{name:string;label?:string;status:string;result:unknown}[]}[]; partner?: PartnerDefinition | null; assistant_profile?:PartnerDefinition|null; effective_tool_ids?: string[]; skill_refs?: SkillRef[]; skill_runs?: {run_id: string; resources: {path: string; characters:number}[]}[]; kb_id?: string | null; kb_ids?: string[]; model_profile_id?: string | null; messages: ChatMessage[]; pending: { stage_name?:string;kind?:string;connector?:string;tool?:string;arguments?:unknown;command?:string;reasons?:{key:string;description:string}[];filename: string; content: string; diff: string }[]; next: string[]; library_mode: number; sources: { chunk_id?: string; path: string; start: number; end: number }[] };
type Config = { model: string; configured: boolean; active_job: Job | null; active_jobs?: Job[] };


function App() {
  const statusText: Record<string, string> = { completed: t("已完成"), waiting_approval: t("待确认"), failed: t("未完成"), stopped: t("已停止"), interrupted: t("可继续"), limited: t("已达本轮上限"), running: t("进行中"), queued: t("排队中"), stopping: t("正在停止"), rejected: t("已取消操作"), conflict: t("操作需核对"), created: t("已创建") };
  useLanguage();
  const [page, setPage] = useState<'chat' | 'knowledge' | 'skills' | 'settings' | 'memory'>('chat');
  const [skills, setSkills] = useState<Skill[]>([]);
  const [skillRefs, setSkillRefs] = useState<SkillRef[]>([]);
  const [connectors,setConnectors]=useState<ConnectorSummary[]>([]);
  const [connectorIds,setConnectorIds]=useState<string[]>([]);
  const refreshConnectors=()=>api<ConnectorSummary[]>('/connectors?summary=true').then(setConnectors);
  const connectorReady=connectorIds.every(id=>connectors.some(c=>c.status==='connected'&&c.tools.some(t=>t.id===id&&t.policy!=='disabled')));
  const [partners, setPartners] = useState<Partner[]>([]);
  const [partner, setPartner] = useState<PartnerDefinition | null>(null);
  const workbenchAssistant = partners.find(p=>p.source==='builtin');
  const currentPartner = partners.find(p=>p.id===partner?.id);
  const partnerReady = !partner || (!!currentPartner?.enabled && !currentPartner.archived);
  const refreshSkills = async () => {const [s,p]=await Promise.all([api<Skill[]>('/skills'),api<Partner[]>('/partners')]);setSkills(s);setPartners(p);await refreshConnectors();};
  const [centerTab, setCenterTab] = useState<CenterTab>('partners');
  const [memoryData, setMemoryData] = useState<MemoryOverview | null>(null);
  const refreshMemory = () => api<MemoryOverview>('/memory').then(setMemoryData);
  const [preferences, setPreferences] = useState<Preferences | null>(null);
  const [modelId, setModelId] = useState('');
  const selectedModelId = modelId || (!partner?workbenchAssistant?.model_profile_id:null) || preferences?.default_chat_id || '';
  const selectedModel = preferences?.profiles.find(p => p.id === selectedModelId && p.kind === 'chat');
  const modelReady = !!selectedModel && (!!selectedModel.api_key_set || ['localhost', '127.0.0.1', '[::1]'].includes(new URL(selectedModel.base_url).hostname));
  const [bases, setBases] = useState<KnowledgeBase[]>([]);
  const [kbIds, setKbIds] = useState<string[]>([]);
  const skillReady = skillRefs.length<=3&&skillRefs.every(ref=>{const skill=skills.find(s=>s.id===ref.id);const revision=skill?.revisions.find(r=>r.revision===ref.revision);return !!skill?.enabled&&!skill.archived&&!!skill.runtime_ready&&!!revision?.compatible&&(!revision.requires_knowledge_base||kbIds.length>0);});
  const scopeReady = kbIds.every(id => bases.some(k => k.id === id && k.status === 'ready'));
  const refreshBases = () => api<KnowledgeBase[]>('/knowledge/bases').then(setBases);
  const [config, setConfig] = useState<Config | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionTotal,setSessionTotal]=useState(0);
  const [sessionHasMore,setSessionHasMore]=useState(false);
  const [sessionQuery,setSessionQuery]=useState('');
  const [projects,setProjects]=useState<Project[]>([]);
  const [selectedProjectId,setSelectedProjectId]=useState<string|null>(null);
  const selectedProject=selectedProjectId?projects.find(project=>project.id===selectedProjectId)||null:null;
  const [creatingProject,setCreatingProject]=useState(false);
  const [projectName,setProjectName]=useState('');
  const [notes, setNotes] = useState<SavedNote[]>([]);
  const [tid, setTid] = useState<string | null>(null);
  const [chat, setChat] = useState<Chat | null>(null);
  const [displayedContextWindow, setDisplayedContextWindow] = useState<ContextWindowView | null>(null);
  const [draft, setDraft] = useState('');
  const [attachments,setAttachments]=useState<ChatAttachment[]>([]);
  const [uploadingImages,setUploadingImages]=useState(false);
  const [editingTurn, setEditingTurn] = useState<EditableTurn | null>(null);
  const [editDraft, setEditDraft] = useState('');
  const [copiedMessageId, setCopiedMessageId] = useState<string | null>(null);
  const [activeJobs, setActiveJobs] = useState<Record<string, Job>>({});
  const [sessionStatuses,setSessionStatuses]=useState<Record<string,string>>({});
  const [starting, setStarting] = useState(false);
  const [progress, setProgress] = useState<string[]>([]);
  const [tab, setTab] = useState<'notes' | 'artifacts'>('notes');
  const [sideOpen, setSideOpen] = useState(window.innerWidth > 850);
  const [source, setSource] = useState<Chunk[] | null>(null);
  const [editing, setEditing] = useState<Partial<SavedNote> | null>(null);
  const [notePreview, setNotePreview] = useState(false);
  const [saving, setSaving] = useState(false);
  const [artifacts, setArtifacts] = useState<string[]>([]);
  const [artifactPreview,setArtifactPreview]=useState<{title:string;content:string}|null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [deleteAnswer, setDeleteAnswer] = useState<ChatMessage | null>(null);
  const imageInput = useRef<HTMLInputElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const tidRef = useRef<string | null>(null);
  const job = tid ? activeJobs[tid] || null : null;
  const busy = !!job || starting;
  const imageReady = !attachments.length || !!selectedModel?.input_modalities?.includes('image');
  const activeAssistantProfile = partner?null:(chat?.assistant_profile || (!tid?workbenchAssistant:null));
  const boundSkill = !!partner?.skill_refs?.length || !!activeAssistantProfile?.skill_refs?.length || ['fixed','dynamic'].includes(partner?.execution_mode||'');
  const report = (e: unknown) => setError(e instanceof Error ? e.message : t("操作未完成。"));
  const missingSession = (e:unknown) => e instanceof ApiError && [400,404].includes(e.status) && typeof e.detail==='string' && e.detail.startsWith('没有找到此会话');

  async function loadSessions(reset=true) {
    const offset=reset?0:sessions.length;
    const query=new URLSearchParams({paged:'true',archived:'false',offset:String(offset),limit:'30'});
    if(sessionQuery)query.set('q',sessionQuery);else query.set('unassigned','true');
    const result=await api<SessionPage>('/sessions?'+query);
    setSessions(current=>reset?result.items:[...current,...result.items]);setSessionTotal(result.total);setSessionHasMore(result.has_more);
  }
  async function refresh() {
    const [s, p, n] = await Promise.all([
      api<SessionPage>('/sessions?paged=true&archived=false&offset=0&limit=30'+(sessionQuery?'&q='+encodeURIComponent(sessionQuery):'&unassigned=true')),
      api<Project[]>('/projects'),api<SavedNote[]>('/notes')]);
    setSessions(s.items);setSessionTotal(s.total);setSessionHasMore(s.has_more);setProjects(p);setNotes(n); await Promise.all([refreshBases(),refreshSkills(),refreshMemory()]);
  }
  async function loadThread(id: string) {
    const [c, a] = await Promise.all([api<Chat>('/sessions/' + id), api<string[]>('/sessions/' + id + '/artifacts')]);
    if (tidRef.current === id) { setChat(c);setSelectedProjectId(c.project_id||null); setArtifacts(a); setKbIds(c.kb_ids ?? (c.kb_id ? [c.kb_id] : [])); setModelId(c.model_profile_id || ''); setSkillRefs(c.skill_refs || []); setPartner(c.partner || null);setConnectorIds(c.connector_tool_ids || []); }
  }
  function select(id: string | null) {
    attachments.forEach(item=>api('/chat-attachments/'+item.id,{method:'DELETE'}).catch(()=>{}));
    setAttachments([]);
    setPage('chat'); tidRef.current = id; setTid(id); setChat(null); setArtifacts([]); setProgress([]); setError('');
    setDisplayedContextWindow(null);
    setEditingTurn(null); setEditDraft(''); setCopiedMessageId(null);
    if (id) { localStorage.setItem('workbench-thread', id); loadThread(id).catch(e=>{if(missingSession(e)&&tidRef.current===id){select(null);return;}report(e);}); }
    else { setSelectedProjectId(null);setConnectorIds([]);setPartner(null); setSkillRefs(workbenchAssistant?.skill_refs || []); setKbIds([]); setModelId(''); localStorage.removeItem('workbench-thread'); composer.current?.focus(); }
  }
  async function searchSessions(query:string){setSessionQuery(query);const params=new URLSearchParams({paged:'true',archived:'false',offset:'0',limit:'30'});if(query)params.set('q',query);else params.set('unassigned','true');const result=await api<SessionPage>('/sessions?'+params);setSessions(result.items);setSessionTotal(result.total);setSessionHasMore(result.has_more)}
  function startNew(projectId:string|null){select(null);setSelectedProjectId(projectId);}
  async function createProject(e?:React.FormEvent){e?.preventDefault();if(!projectName.trim())return;setSaving(true);try{const created=await api<Project>('/projects',{method:'POST',body:JSON.stringify({name:projectName})});setCreatingProject(false);setProjectName('');await refresh();startNew(created.id)}catch(e){report(e)}finally{setSaving(false)}}
  const memoryChanged = !!tid && !['fixed','dynamic'].includes(partner?.execution_mode||'') && !!memoryData &&
    (!!chat?.legacy_mixed_memory || (!!chat?.memory_binding && chat.memory_binding!==memoryData.active_profile_id) || (!!chat?.memory?.space_id && chat.memory.space_id!==memoryData.current_space_id));
  function usePartner(p: PartnerDefinition) {
    if (busy || chat?.next.length) return;
    if (partner?.id===p.id && partner.version===p.version) {setPage('chat');return;}
    const hadThread=!!tid;
    const selectedKnowledge = [...kbIds];
    select(null);
    setKbIds(selectedKnowledge);
    if(p.source!=='builtin') {setPartner(p);setSkillRefs(p.skill_refs || []);setModelId(p.model_profile_id || preferences?.default_chat_id || '');}
    else {setPartner(null);setSkillRefs(p.skill_refs || []);setModelId('');}
    if(hadThread)setNotice(t('已新建对话，原会话已保留。'));
  }
  useEffect(() => {
    refresh().catch(report);
    api<Preferences>('/settings').then(p => { setPreferences(p); setLanguage(p.language); }).catch(report);
    api<Config>('/config').then(c => {
      setConfig(c);
      const running = c.active_jobs || (c.active_job ? [c.active_job] : []);
      setActiveJobs(Object.fromEntries(running.map(item => [item.thread_id, item])));
      setSessionStatuses(Object.fromEntries(running.map(item => [item.thread_id,item.status])));
      const remembered = localStorage.getItem('workbench-thread');
      if (remembered) select(remembered);
    }).catch(report);
  }, []);
  useEffect(()=>{if(!tid&&!partner&&workbenchAssistant?.skill_refs?.length)setSkillRefs(workbenchAssistant.skill_refs);},[workbenchAssistant?.profile_version,tid,partner?.id]);
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }, [chat, progress.length]);
  useEffect(() => {
    if (!busy) setDisplayedContextWindow(tid ? (chat?.context_window || contextWindowForProfile(selectedModel)) : null);
  }, [
    tid,
    busy,
    chat?.context_window,
    selectedModel?.id,
    selectedModel?.model_context_window,
    selectedModel?.context_window,
    selectedModel?.max_tokens,
    selectedModel?.context_compaction_trigger,
    selectedModel?.context_compaction_target,
  ]);
  useEffect(() => { if (notice) { const timer = setTimeout(() => setNotice(''), 4500); return () => clearTimeout(timer); } }, [notice]);
  useEffect(() => {
    const close = (e: KeyboardEvent) => { if (e.key === 'Escape') { setSource(null); setEditing(null); setDeleteAnswer(null); } };
    window.addEventListener('keydown', close); return () => window.removeEventListener('keydown', close);
  }, []);
  useEffect(() => {
    const running = Object.values(activeJobs);
    if (!running.length) return;
    const streams = running.map(activeJob => {
      const stream = new EventSource('/api/jobs/' + activeJob.id + '/events');
      let finished = false;
      const current = () => tidRef.current === activeJob.thread_id;
      const finish = () => {
        if (finished) return; finished = true; stream.close();
        setActiveJobs(items => {
          if (items[activeJob.thread_id]?.id !== activeJob.id) return items;
          const next = {...items}; delete next[activeJob.thread_id]; return next;
        });
        refresh().catch(report);
        if (current()) loadThread(activeJob.thread_id).catch(report);
        api<Job>('/jobs/' + activeJob.id).then(j => { setSessionStatuses(items=>({...items,[j.thread_id]:j.status}));if (j.error && current()) setError(t(j.error)); }).catch(report);
      };
      stream.onmessage = (event) => {
        const item = JSON.parse(event.data);
        if (current() && item.kind === 'skill_loaded') setProgress(p => [...p.slice(-9), t("已加载技能：{0}", item.name)]);
        if (current() && item.kind === 'skill_resource') setProgress(p => [...p.slice(-9), t("已读取技能资源：{0}", item.path)]);
        if (current() && item.kind === 'progress') setProgress(p => {
          const label = t(item.label).replace(/ · 第 \d+ 步$/, '');
          const same = (value: string) => value.replace(/ · 第 \d+ 步$/, '') === label;
          return same(p.at(-1) || '') ? p : [...p.filter(value => !same(value)).slice(-9), label];
        });
        if (item.kind === 'started') {setActiveJobs(items => items[activeJob.thread_id]?.id === activeJob.id ? {...items,[activeJob.thread_id]:{...items[activeJob.thread_id],status:'running',queue_reason:undefined}} : items);setSessionStatuses(items=>({...items,[activeJob.thread_id]:'running'}));}
        if (current() && item.kind === 'problem') setError(t(item.message));
        if (current() && (item.kind === 'stage' || item.kind === 'message')) loadThread(activeJob.thread_id).catch(() => {});
        if (item.kind === 'done') {setSessionStatuses(items=>({...items,[activeJob.thread_id]:item.status||'completed'}));finish();}
      };
      stream.addEventListener('closed', finish);
      stream.onerror = () => { if (current()) setProgress(p => p.at(-1) === t("连接暂时断开，正在重新连接…") ? p : [...p, t("连接暂时断开，正在重新连接…")]); };
      return stream;
    });
    return () => streams.forEach(stream => stream.close());
  }, [Object.values(activeJobs).map(item => item.id).sort().join(',')]);

  async function send(e?: React.FormEvent) {
    e?.preventDefault(); if (!draft.trim() || busy || editingTurn || !modelReady || !scopeReady || !skillReady || !partnerReady || !imageReady) return;
    setStarting(true); setError(''); setProgress([kbIds.length ? t("开始查找资料…") : t("正在处理…")]);
    try {
      let baseChat = chat;
      if (tid && chat?.next.length && !memoryChanged) {
        baseChat = await api<Chat>('/sessions/' + tid + '/abandon', { method: 'POST' });
        setChat(baseChat);
      }
      const sentAttachments=attachments;
      const payload = { text: draft, thread_id: memoryChanged ? null : tid, project_id:memoryChanged?(chat?.project_id||null):(!tid?selectedProjectId:null), capability_id: partner?.id || 'workbench-assistant', capability_version: partner?.version || '1.0.0', kb_ids: kbIds, model_profile_id: selectedModelId || null, connector_tool_ids:connectorIds, skill_refs: skillRefs.map(({id,revision}) => ({id,revision})), attachment_ids:sentAttachments.map(item=>item.id) };
      const j = await api<Job>('/jobs', { method: 'POST', body: JSON.stringify(payload) });
      tidRef.current = j.thread_id; setTid(j.thread_id); localStorage.setItem('workbench-thread', j.thread_id);
      if(memoryChanged)setNotice(t('已新建对话，原历史对话保留。'));
      setChat(previous => { const c=memoryChanged?null:(baseChat || previous);return ({ ...(c || { id: j.thread_id, title: draft, pending: [], next: [], sources: [], library_mode: 1, kb_ids: kbIds }), editable_turn:null, messages: [...(c?.messages || []), { id: 'pending-user-'+j.id, role: 'human', content: draft, attachments:sentAttachments, run_id:j.snapshot?.run_id }], status: 'running' } as Chat);});
      setDraft('');setAttachments([]); setActiveJobs(items => ({...items, [j.thread_id]: j}));setSessionStatuses(items=>({...items,[j.thread_id]:j.status}));
      if(j.status==='queued'&&j.queue_reason)setProgress([t(j.queue_reason)]);
    } catch (e) { report(e); } finally { setStarting(false); }
  }
  async function resume(decision?: boolean | 'session') {
    if (!tid || busy) return; setStarting(true); setError(''); setProgress([t("继续任务…")]);
    try { setEditingTurn(null); setEditDraft(''); const j=await api<Job>('/sessions/' + tid + '/resume', { method: 'POST', body: JSON.stringify({ decision: decision ?? null }) });setActiveJobs(items=>({...items,[j.thread_id]:j}));setSessionStatuses(items=>({...items,[j.thread_id]:j.status})); }
    catch (e) { report(e); } finally { setStarting(false); }
  }
  function editStoppedTurn(turn?: EditableTurn | null) {
    if (!turn || busy) return;
    setEditingTurn(turn); setEditDraft(turn.content); setError('');
  }
  function cancelTurnEdit() {
    setEditingTurn(null); setEditDraft('');
  }
  async function sendRevision(e?: React.FormEvent) {
    e?.preventDefault();
    if (!editingTurn || !tid || !editDraft.trim() || busy || memoryChanged || !modelReady || !scopeReady || !skillReady || !partnerReady || !connectorReady || (!!editingTurn.attachments?.length&&!selectedModel?.input_modalities?.includes('image'))) return;
    setStarting(true); setError(''); setProgress([kbIds.length ? t('开始查找资料…') : t('正在处理…')]);
    try {
      const payload = { text: editDraft, thread_id: tid, capability_id: partner?.id || 'workbench-assistant', capability_version: partner?.version || '1.0.0', kb_ids: kbIds, model_profile_id: selectedModelId || null, connector_tool_ids:connectorIds, skill_refs:skillRefs.map(({id,revision})=>({id,revision})),attachment_ids:(editingTurn.attachments||[]).map(item=>item.id) };
      const j = await api<Job>('/sessions/' + tid + '/revise', { method:'POST', body:JSON.stringify(payload) });
      setChat(previous => previous ? ({...previous,editable_turn:null,status:'running',messages:[...previous.messages.map(message=>editingTurn.run_id&&message.run_id===editingTurn.run_id?{...message,turn_state:'superseded' as const}:message),{id:'pending-user-'+j.id,role:'human',content:editDraft,attachments:editingTurn.attachments,run_id:j.snapshot?.run_id,turn_state:'revision'}]}) : previous);
      setEditingTurn(null); setEditDraft(''); setActiveJobs(items=>({...items,[j.thread_id]:j}));setSessionStatuses(items=>({...items,[j.thread_id]:j.status}));
    } catch (e) { report(e); } finally { setStarting(false); }
  }
  async function addImages(files:File[]) {
    if(!files.length)return;
    const available=4-attachments.length;
    if(available<=0){setError(t('每次最多添加 4 张图片。'));return;}
    const selected=files.slice(0,available);
    if(attachments.reduce((total,item)=>total+item.bytes,0)+selected.reduce((total,file)=>total+file.size,0)>24*1024*1024){setError(t('本次图片总大小不能超过 24 MiB。'));return;}
    setUploadingImages(true);setError('');
    const added:ChatAttachment[]=[];const problems:string[]=[];
    for(const file of selected){
      try{
        if(!['image/jpeg','image/png','image/webp','image/gif'].includes(file.type))throw new Error(t('仅支持 JPEG、PNG、WebP 或 GIF 图片。'));
        if(file.size>12*1024*1024)throw new Error(t('单张图片不能超过 12 MiB。'));
        const form=new FormData();form.append('file',file,file.name||'pasted-image.png');
        added.push(await api<ChatAttachment>('/chat-attachments',{method:'POST',body:form}));
      }catch(e){problems.push(`${file.name||t('粘贴的图片')}：${(e as Error).message}`);}
    }
    setAttachments(current=>[...current,...added].slice(0,4));setUploadingImages(false);
    if(imageInput.current)imageInput.current.value='';
    if(files.length>available)problems.push(t('每次最多添加 4 张图片。'));
    if(problems.length)setError(problems.join('\n'));
  }
  function pasteImages(event:React.ClipboardEvent<HTMLTextAreaElement>){
    const files=Array.from(event.clipboardData.items).filter(item=>item.kind==='file'&&item.type.startsWith('image/')).map(item=>item.getAsFile()).filter((file):file is File=>!!file);
    if(files.length){event.preventDefault();addImages(files).catch(report);}
  }
  async function removeImage(item:ChatAttachment){
    setAttachments(current=>current.filter(value=>value.id!==item.id));
    try{await api('/chat-attachments/'+item.id,{method:'DELETE'});}catch(e){report(e);}
  }
  async function copyMessage(message: ChatMessage) {
    try {
      await navigator.clipboard.writeText(message.content);
    } catch {
      const temporary=document.createElement('textarea');temporary.value=message.content;temporary.style.position='fixed';temporary.style.opacity='0';document.body.appendChild(temporary);temporary.select();document.execCommand('copy');temporary.remove();
    }
    setCopiedMessageId(message.id); window.setTimeout(()=>setCopiedMessageId(current=>current===message.id?null:current),1400);
  }
  async function regenerateAnswer(message: ChatMessage) {
    if (!tid || busy) return;
    setStarting(true); setError(''); setProgress([t('正在重新生成回答…')]);
    try {
      const j=await api<Job>('/sessions/'+tid+'/answers/'+encodeURIComponent(message.id)+'/regenerate',{method:'POST'});
      const original=chat?.messages.find(item=>item.role==='human'&&item.run_id===message.run_id);
      tidRef.current=j.thread_id;setTid(j.thread_id);localStorage.setItem('workbench-thread',j.thread_id);
      if(j.branched){
        setChat({id:j.thread_id,title:original?.content||t('重新生成的回答'),status:'running',mode:'agent',updated:j.created||'',pending:[],next:[],sources:[],library_mode:1,kb_ids:kbIds,model_profile_id:selectedModelId,messages:original?[{...original,id:'pending-user-'+j.id,run_id:j.snapshot?.run_id}]:[]} as Chat);
        setNotice(t('已从历史回答创建新对话。'));
      }else{
        setChat(previous=>previous?{...previous,status:'running',messages:[...previous.messages.map(item=>item.run_id===message.run_id?{...item,turn_state:'superseded' as const}:item),...(original?[{...original,id:'pending-user-'+j.id,run_id:j.snapshot?.run_id,turn_state:'revision' as const}]:[])]}:previous);
      }
      setActiveJobs(items=>({...items,[j.thread_id]:j}));setSessionStatuses(items=>({...items,[j.thread_id]:j.status}));
      if(j.status==='queued'&&j.queue_reason)setProgress([t(j.queue_reason)]);
      refresh().catch(()=>{});
    } catch(e){report(e);} finally{setStarting(false);}
  }
  async function confirmDeleteAnswer() {
    if(!tid||!deleteAnswer||busy)return;
    const target=deleteAnswer;setStarting(true);setError('');
    try{
      const updated=await api<Chat>('/sessions/'+tid+'/answers/'+encodeURIComponent(target.id),{method:'DELETE'});
      setChat(updated);setDeleteAnswer(null);setNotice(t('回答已删除，后续对话不会再使用这一轮。'));refresh().catch(()=>{});
    }catch(e){report(e);}finally{setStarting(false);}
  }
  function tokenLabel(value?:number){if(!value)return 'Token —';return value>=1000?`${(value/1000).toFixed(value>=10000?1:2)}k tokens`:`${value} tokens`;}
  function durationLabel(value?:number){if(value===undefined||value===null)return t('用时 —');if(value<60)return `${value}s`;return `${Math.floor(value/60)}m ${value%60}s`;}
  function costLabel(trace?:ReasoningTrace){const metrics=trace?.metrics;if(!metrics||metrics.cost===null)return t('费用不可用');const symbol=metrics.currency==='USD'?'$':'¥';if(metrics.cost_status==='local')return `${symbol}0`;const digits=metrics.cost<0.01?4:2;return `${metrics.cost_status==='estimated'||metrics.cost_status==='partial'?'≈':''}${symbol}${metrics.cost.toFixed(digits)}`;}
  function metricsTitle(trace?:ReasoningTrace){const metrics=trace?.metrics;if(!metrics)return t('历史回答可能没有完整用量记录');if(metrics.total_tokens&&!metrics.input_tokens&&!metrics.output_tokens)return t('供应商仅返回总 Token，无法拆分输入、输出或精确计算费用。');const usage=t('输入 {0} · 输出 {1} · 缓存读取 {2} · 缓存写入 {3} · {4} 次模型调用',metrics.input_tokens,metrics.output_tokens,metrics.cached_input_tokens,metrics.cache_write_tokens||0,metrics.model_calls);if(metrics.cost_status==='unavailable')return usage+' · '+t('没有匹配到可靠的官方价格');if(metrics.cost_status==='local')return usage+' · '+t('本地模型 API 费用为零');return usage+' · '+t(metrics.cost_partial?'部分用量缺失，费用为部分估算':'按调用时冻结的官方价格估算');}
  async function openSource(id: string) { try { setSource([await api<Chunk>('/sources/' + id)]); } catch (e) { report(e); } }
  function markdown(text: string) {
    let count = 0;
    const formatted = text.replace(/\[\[(c_[a-f0-9]{24})\]\]/g, (_, id) => `[${++count}](#source=${id})`);
    return <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ href, children }) => href?.startsWith('#source=') ? <button className="citation" title={t("查看引用原文")} onClick={() => openSource(href.slice(8))}>{children}</button> : <a href={href} target="_blank" rel="noreferrer">{children}</a> }}>{formatted}</ReactMarkdown>;
  }
  function renderMessage(m: ChatMessage, i: number) {
    const human=m.role==='human';
    const inlineEditing=human&&editingTurn?.message_id===m.id;
    const editable=human&&chat?.editable_turn?.message_id===m.id;
    const currentRun=job?.snapshot?.run_id;
    const reasoning=!human ? (m.run_id ? chat?.reasoning_runs?.[m.run_id] : undefined) || {
      run_id:m.run_id,status:'completed',mode:'thinking' as const,
      steps:[t('已结合当前对话形成回答')],
    } : undefined;
    return <article key={m.id||i} className={'message '+(human?'user-message':'assistant-message')+(m.turn_state==='superseded'?' superseded-message':'')+(inlineEditing?' inline-editing':'')}>
      <div className="message-label"><span>{human?t('你'):<><Leaf size={15}/> {t('工作台')}</>}</span>{m.turn_state==='superseded'&&<span className="turn-badge">{t('已停止 · 已修订')}</span>}{m.turn_state==='revision'&&<span className="turn-badge revision">{t('修订后的提问')}</span>}</div>
      {human&&!!m.skill_refs?.length&&<div className="message-skill">{m.skill_refs.map(ref=><span key={ref.id}><Sparkles size={12}/>{t('本轮技能')}：{ref.name} · {versionLabel(ref)}</span>)}<details><summary>{t('技能加载记录')}</summary><p>{t('技能说明已加载，以下为实际读取的参考文件。')}</p>{(chat?.skill_runs?.find(run=>run.run_id===m.run_id)?.resources||[]).map((r,index)=><p key={index}>{r.path} · {r.characters} {t('字符')}</p>)}{!chat?.skill_runs?.find(run=>run.run_id===m.run_id)?.resources.length&&<p>{t('本轮尚未读取技能参考文件。')}</p>}</details></div>}
      {reasoning&&!(busy&&m.run_id===currentRun)&&<ReasoningPanel trace={reasoning}/>}
      {human&&!!m.attachments?.length&&<div className="message-attachments">{m.attachments.map(item=><a key={item.id} href={item.url} target="_blank" rel="noreferrer"><img src={item.url} alt={item.name}/></a>)}</div>}
      {inlineEditing?<form className="inline-message-editor" onSubmit={sendRevision}>
        <textarea autoFocus aria-label={t('编辑提问')} value={editDraft} onChange={e=>setEditDraft(e.target.value)} maxLength={10000} rows={4} onKeyDown={e=>{if(e.key==='Escape'){e.preventDefault();cancelTurnEdit();}else if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();sendRevision();}}}/>
        <div className="inline-edit-footer"><span>{t('发送后会创建修订轮次；停止前已完成的操作会保留。')}</span><div><button type="button" className="secondary" onClick={cancelTurnEdit}>{t('取消')}</button><button type="submit" className="primary" disabled={!editDraft.trim()||starting}>{starting?<LoaderCircle className="spin" size={15}/>:null}{t('发送')}</button></div></div>
      </form>:<div className="markdown">{markdown(m.content)}</div>}
      {human&&!inlineEditing&&<div className="message-actions"><span className="copy-feedback" aria-live="polite">{copiedMessageId===m.id?t('已复制'):''}</span><button type="button" className="message-action" aria-label={t('复制提问')} title={t('复制')} onClick={()=>copyMessage(m)}>{copiedMessageId===m.id?<Check size={15}/>:<Copy size={15}/>}</button>{editable&&<button type="button" className="message-action" aria-label={t('编辑提问')} title={t('编辑')} onClick={()=>editStoppedTurn(chat?.editable_turn)}><Pencil size={15}/></button>}</div>}
      {m.role==='ai'&&<div className="answer-footer"><div className="answer-actions"><button className="message-action" aria-label={t('复制回答')} title={t('复制')} onClick={()=>copyMessage(m)}>{copiedMessageId===m.id?<Check size={15}/>:<Copy size={15}/>}</button><button className="message-action" disabled={busy||m.turn_state==='superseded'} aria-label={t('重新生成回答')} title={m.turn_state==='superseded'?t('已被新版本替换'):t('重新生成')} onClick={()=>regenerateAnswer(m)}><RefreshCw size={15}/></button><button className="message-action" disabled={busy} aria-label={t('删除回答')} title={t('删除')} onClick={()=>setDeleteAnswer(m)}><Trash2 size={15}/></button><button className="message-action" aria-label={t('保存为笔记')} title={t('保存为笔记')} onClick={()=>{setNotePreview(false);setEditing({title:t('对话笔记'),content:m.content,thread_id:tid||undefined});}}><BookMarked size={15}/></button><span className="copy-feedback" aria-live="polite">{copiedMessageId===m.id?t('已复制'):''}</span></div><div className="answer-metrics" title={metricsTitle(reasoning)}><span>{tokenLabel(reasoning?.metrics?.total_tokens)}</span><i>·</i><span>{costLabel(reasoning)}</span><i>·</i><span>{durationLabel(reasoning?.metrics?.duration_seconds??reasoning?.duration_seconds)}</span></div></div>}
    </article>;
  }
  async function saveNote() {
    if (!editing?.title?.trim() || !editing?.content?.trim()) { setError(t("请填写笔记标题和内容。")); return; }
    setSaving(true);
    try {
      await api('/notes' + (editing.id ? '/' + editing.id : ''), { method: editing.id ? 'PUT' : 'POST', body: JSON.stringify({ title: editing.title, content: editing.content, thread_id: editing.thread_id || tid }) });
      setEditing(null); setTab('notes'); setSideOpen(true); setNotice(t("笔记已保存")); await refresh();
    } catch (e) { report(e); } finally { setSaving(false); }
  }
  async function download(title: string, text: string) {
    try {
      const ids = [...new Set([...text.matchAll(/\[\[(c_[a-f0-9]{24})\]\]/g)].map(m => m[1]))];
      const refs = await Promise.all(ids.map(id => api<Chunk>('/sources/' + id)));
      let content = text.replace(/\[\[(c_[a-f0-9]{24})\]\]/g, (_, id) => `[^${ids.indexOf(id) + 1}]`);
      if (refs.length) content += t("\n\n## 引用原文\n\n") + refs.map((r, i) => t("[^{0}]: 《{1}》{2}，第 {3} 段，版本 {4}。\n\n    {5}", i + 1, r.title, r.page ? t("，第 {0} 页", r.page) : '', r.paragraph, r.version.slice(0, 12), r.text.trim().replace(/\n/g, '\n    '))).join('\n\n');
      const url = URL.createObjectURL(new Blob([content], { type: 'text/markdown;charset=utf-8' }));
      const a = document.createElement('a'); a.href = url; a.download = title.endsWith('.md') ? title : title + '.md'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) { report(e); }
  }
  function downloadArtifact(title:string,content:string){const type=title.endsWith('.html')?'text/html':'text/plain';const url=URL.createObjectURL(new Blob([content],{type:type+';charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=title;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}

  const emptyConversation = !chat?.messages?.length && !busy;

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><img src={auroLogo} alt="" aria-hidden="true"/></div><div><b>{t("Auro")}</b><small>{t("Agentic for everything.")}</small></div></div>
      <button className="new-chat" onClick={() => startNew(null)}><Plus size={18}/> {t("新对话")}<span>＋</span></button>
      <ConversationSidebar sessions={sessions} projects={projects} selectedId={tid} selectedProjectId={selectedProjectId} total={sessionTotal} hasMore={sessionHasMore} busy={busy} query={sessionQuery} statusBySession={sessionStatuses} onSelect={select} onNewInProject={startNew} onProjectUnavailable={id=>{if(selectedProjectId===id)setSelectedProjectId(null)}} onChanged={refresh} onMore={()=>loadSessions(false).catch(report)} onCreateProject={()=>setCreatingProject(true)} onSearch={query=>searchSessions(query).catch(report)}/>
      <div className="sidebar-bottom"><button className={"skill-nav " + (page === "skills" ? "active" : "")} aria-label={t("技能广场")} title={t("技能广场")} onClick={() => setPage("skills")}><Sparkles size={17}/> {t("技能广场")}</button><button aria-label={t("知识仓库")} title={t("知识仓库")} className={"knowledge-nav " + (page === "knowledge" ? "active" : "")} onClick={() => setPage("knowledge")}><BookOpen size={17}/> {t("知识仓库")}</button><button className={"memory-nav " + (page === "memory" ? "active" : "")} aria-label={t("记忆中心")} title={t("记忆中心")} onClick={() => setPage("memory")}><Brain size={17}/> {t("记忆中心")}</button><button className={"settings-nav " + (page === "settings" ? "active" : "")} aria-label={t("设置")} title={t("设置")} onClick={() => setPage("settings")}><Settings2 size={17}/> {t("设置")}</button><div className="connection app-version">{t("版本号")}：v1.0.0</div></div>
    </aside>

    {page === 'memory' ? <MemoryCenter preferences={preferences} chatRunning={busy || !!chat?.next.length} onClose={() => setPage('chat')} onChanged={() => refreshMemory().catch(report)} onSource={id => { if(busy){setError(t('请等待当前对话完成后再打开来源。'));return;}select(id);}}/> : page === 'skills' ? <SkillCenter tab={centerTab} onTab={setCenterTab} onClose={() => setPage('chat')} onSkillsChanged={refreshSkills} onUsePartner={usePartner} locked={busy || !!chat?.next.length} onUse={ref => {if(boundSkill){select(null);setNotice(t('已新建通用助手对话以使用所选技能。'));}setSkillRefs([ref]);setPage('chat');}}/> : page === 'settings' ? (preferences ? <SettingsPage initial={preferences} onClose={() => setPage('chat')} onArchiveChanged={refresh} onChanged={p => { setPreferences(p); if (modelId && !p.profiles.some(m => m.id === modelId)) setModelId(p.default_chat_id); api<Config>('/config').then(setConfig).catch(report); refreshBases().catch(report); }}/> : <main className="knowledge-center">{t("正在加载设置…")}</main>) : page === 'knowledge' ? <KnowledgeCenter onClose={() => setPage('chat')} onChanged={() => refreshBases().catch(report)} onChat={kb => { if (busy) { setError(t("请等待当前对话完成后再切换知识库。")); setPage('chat'); return; } select(null); setKbIds([kb.id]); }} /> : <>
    <main className={"main-area" + (emptyConversation ? " empty-conversation" : "")}>
      <header className="topbar"><div><span className="eyebrow">PERSONAL WORKSPACE</span><h1>{tid ? t("走向智能世界") : t("今天，学点什么？")}</h1></div><div className="top-actions"><button className="icon-button" title={sideOpen ? t("收起资料栏") : t("展开资料栏")} onClick={() => setSideOpen(!sideOpen)}>{sideOpen ? <PanelRightClose size={20}/> : <PanelRightOpen size={20}/>}</button></div></header>
      {error && <div role="alert" className="alert"><span>{error}</span><button aria-label={t("关闭错误提示")} onClick={() => setError('')}><X size={16}/></button></div>}
      <section className="conversation" aria-label={t("对话内容")}>
        {!emptyConversation && <div className="message-stack">
          {chat?.messages.map(renderMessage)}
          {busy && <ReasoningPanel active trace={{
            run_id:job?.snapshot?.run_id,status:job?.status||'running',started_at:job?.created,
            mode:['fixed','dynamic'].includes(partner?.execution_mode||'')?'executing':progress.some(item=>/搜索|检索|资料|网页|论文|search|fetch/i.test(item))?'exploring':'thinking',
            steps:progress.length?progress:[t('正在分析问题并组织回答')],
          }}/>}
          {!!chat?.workflow_runs?.length&&<WorkflowRunCards runs={chat.workflow_runs} renderMarkdown={markdown}/>}
          {chat?.connector_runs?.filter(r=>r.tools.length).map((r,i)=><details className="mcp-history" key={r.run_id || i}><summary>{t('连接器调用记录')} · {r.tools.length}</summary>{r.tools.map((tool,j)=><details key={j}><summary>{tool.label || tool.name} · {t(tool.status==='error'?'失败':'已返回')}</summary><pre>{JSON.stringify(tool.result,null,2)}</pre></details>)}</details>)}
          {!busy && chat?.pending.map((p, i) => <section className="approval" key={i}><span className="eyebrow">{t("需要你的决定")}</span>{p.stage_name&&<p>{t('当前阶段')}：{p.stage_name}</p>}{p.kind==='terminal'?<><h3>{t('确认终端命令')}</h3><p>{t('工作台检测到这条命令可能修改本机环境或执行外部内容。')}</p><pre>{p.command}</pre>{p.reasons?.length?<ul>{p.reasons.map(reason=><li key={reason.key}>{t(reason.description)}</li>)}</ul>:null}<div className="button-row"><button className="primary" onClick={()=>resume(true)}>{t('允许一次')}</button><button className="secondary" onClick={()=>resume('session')}>{t('本次会话允许')}</button><button className="secondary" onClick={()=>resume(false)}>{t('拒绝')}</button></div></>:p.kind==='mcp'?<><h3>{t('确认外部操作')}</h3><p>{p.connector} · <b>{p.tool}</b></p><p>{t('请核对目标工具和参数。批准只适用于这次调用；写入超时后不会自动重试。')}</p><pre>{JSON.stringify(p.arguments,null,2)}</pre><div className="button-row"><button className="primary" onClick={()=>resume(true)}>{t('批准执行')}</button><button className="secondary" onClick={()=>resume(false)}>{t('取消操作')}</button></div></>:<><h3>{t("覆盖「")}{p.filename}」？</h3><p>{t("同名文件已存在。请查看新草稿和变化，再决定是否保存。")}</p><details><summary>{t("查看完整新草稿")}</summary><div className="markdown">{markdown(p.content)}</div></details><details><summary>{t("查看与原文的差异")}</summary><pre>{p.diff}</pre></details><div className="button-row"><button className="primary" onClick={() => resume(true)}>{t("批准覆盖")}</button><button className="secondary" onClick={() => resume(false)}>{t("保留原文件")}</button></div></>}</section>)}
          {!busy && chat && chat.next.length > 0 && !chat.pending.length && <div className="resume-box"><span>{t("任务已停止，当前进度已保留。可以继续原任务，或使用上一条提问下方的编辑按钮重新发送。")}</span><div className="resume-actions"><button className="secondary" onClick={() => resume()}>{t("继续原任务")}</button></div></div>}
          {!busy && chat && <div className="end-label">{statusText[chat.status] || chat.status}{chat.library_mode === 0 ? t(" · M1 历史会话（引用采用文件行号）") : ''}</div>}
        </div>}
        <div ref={bottom}/>
      </section>
      <div className={"composer-wrap" + (emptyConversation ? " centered-composer" : "")}>{emptyConversation&&<section className="chat-welcome"><img src={chatWelcomeLogo} alt=""/><h2>{t('今天，想探索点什么？')}</h2></section>}{!tid&&!busy&&!skillRefs.length&&(!partner||partner.source==='builtin')&&draft.trim().length>=2&&<CapabilitySuggestions text={draft} partners={partners} onSelect={usePartner}/>}{partner?.execution_mode==='fixed'&&<WorkflowToolPreview partner={partner} kbIds={kbIds} connectorIds={connectorIds} modelId={selectedModelId} locked={busy||!!chat?.next.length} onConfigure={()=>{setCenterTab('partners');setPage('skills');}}/>}{partner && <div className="partner-context"><details><summary>{partner.name} · {t(partner.execution_mode==='dynamic'?'团队配置':partner.execution_mode==='fixed'?'流程配置':'助手配置')} · {partnerVersionLabel(partner)}</summary><pre>{partner.instructions}</pre><p>{t('允许使用的工具')}：{partner.tool_ids.join(', ') || t('无')}</p>{chat?.effective_tool_ids && <p>{t('上轮实际工具')}：{chat.effective_tool_ids.join(', ') || t('无')}</p>}<p>{t('绑定技能')}：{partner.skill_refs?.map(r=>skills.find(s=>s.id===r.id)?.display_name || r.name || r.id).join(', ') || t('不绑定，由对话中选择')}</p>{boundSkill && <p>{t(['fixed','dynamic'].includes(partner.execution_mode||'')?'协作伙伴按成员配置技能，请到技能广场编辑。':'此伙伴已固定技能版本；更换技能请编辑伙伴并新建对话。')}</p>}</details>{!partnerReady && <p role="alert">{t('此伙伴已停用或归档，请新建对话选择其他伙伴。')}</p>}{currentPartner && currentPartner.version!==partner.version && <p>{t('伙伴已有新版本，当前会话保留原配置。')}</p>}{!!partner.suggested_kb_ids?.length && <div><span>{t('推荐知识库')}：{partner.suggested_kb_ids.map(id=>bases.find(k=>k.id===id)?.name || t('知识库不可用')).join('、')}</span><div className="button-row"><button type="button" className="secondary" disabled={busy || !!chat?.next.length || !partner.suggested_kb_ids.some(id=>bases.some(k=>k.id===id&&k.status==='ready'))} onClick={()=>setKbIds(ids=>[...new Set([...ids,...(partner.suggested_kb_ids || []).filter(id=>bases.some(k=>k.id===id&&k.status==='ready'))])])}>{t('选择可用的推荐知识库')}</button></div></div>}{!chat?.messages.length && <div className="partner-examples">{partner.examples?.map((q,i)=><button type="button" key={i} onClick={()=>{setDraft(q);composer.current?.focus();}}>{q}</button>)}</div>}</div>}{!partner&&!tid&&activeAssistantProfile&&(!!activeAssistantProfile.suggested_kb_ids?.length||!!activeAssistantProfile.examples?.length)&&<div className="partner-context builtin-assistant-suggestions">{!!activeAssistantProfile.suggested_kb_ids?.length&&<div><span>{t('推荐知识库')}：{activeAssistantProfile.suggested_kb_ids.map(id=>bases.find(k=>k.id===id)?.name||t('知识库不可用')).join('、')}</span><div className="button-row"><button type="button" className="secondary" disabled={busy||!activeAssistantProfile.suggested_kb_ids.some(id=>bases.some(k=>k.id===id&&k.status==='ready'))} onClick={()=>setKbIds(ids=>[...new Set([...ids,...(activeAssistantProfile.suggested_kb_ids||[]).filter(id=>bases.some(k=>k.id===id&&k.status==='ready'))])])}>{t('选择可用的推荐知识库')}</button></div></div>}{!!activeAssistantProfile.examples?.length&&<div className="partner-examples">{activeAssistantProfile.examples.map((q,i)=><button type="button" key={i} onClick={()=>{setDraft(q);composer.current?.focus();}}>{q}</button>)}</div>}</div>}<form className="composer" onSubmit={send}>{!tid&&selectedProject&&<div className="composer-project-chip"><button type="button" aria-label={t("取消归入当前项目")} title={t("取消归入当前项目")} onClick={()=>setSelectedProjectId(null)}><X size={13}/></button><span>{selectedProject.name}</span></div>}{attachments.length>0&&<div className="composer-image-strip">{attachments.map(item=><div className="composer-image-preview" key={item.id}><img src={item.url} alt={item.name}/><button type="button" aria-label={t('移除图片')} title={t('移除图片')} onClick={()=>removeImage(item)}><X size={13}/></button><span>{item.name}</span></div>)}</div>}<textarea ref={composer} aria-label={t("输入问题")} title={t("Enter 发送 · Shift + Enter 换行")} placeholder={kbIds.length ? t("向所选知识库提问…") : t("输入问题，或选择知识库后提问…")} value={draft} onChange={e => setDraft(e.target.value)} onPaste={pasteImages} disabled={!!editingTurn} maxLength={10000} rows={2} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); } }}/><div className="composer-bottom"><ComposerContextMenu partners={partners} partner={partner} onPartner={usePartner} skills={skills} skillRefs={skillRefs} onSkills={setSkillRefs} boundSkill={boundSkill} connectors={connectors} connectorIds={connectorIds} onConnectors={setConnectorIds} onRefreshConnectors={()=>refreshConnectors().catch(report)} bases={bases} kbIds={kbIds} onKnowledge={setKbIds} locked={busy || !!chat?.next.length} onManage={section=>{if(section==='knowledge')setPage('knowledge');else{setCenterTab(section);setPage('skills');}}}/><input ref={imageInput} type="file" accept="image/jpeg,image/png,image/webp,image/gif" multiple hidden onChange={e=>addImages(Array.from(e.target.files||[])).catch(report)}/><button type="button" className="composer-image-button" disabled={busy||!!editingTurn||uploadingImages||attachments.length>=4} aria-label={t('添加图片')} title={t('粘贴或选择图片')} onClick={()=>imageInput.current?.click()}>{uploadingImages?<LoaderCircle className="spin" size={17}/>:<ImagePlus size={17}/>}</button><div className="composer-actions"><ModelPicker profiles={preferences?.profiles.filter(p => p.kind === 'chat') || []} value={selectedModelId} defaultId={preferences?.default_chat_id || ''} busy={busy} onChange={setModelId} onManage={() => setPage('settings')}/><ContextWindowChip view={displayedContextWindow} onManage={() => setPage('settings')}/>{job ? <button type="button" className="send-button stop" title={t("停止任务")} aria-label={t("停止任务")} onClick={() => api('/jobs/' + job.id + '/cancel', { method: 'POST' }).then(() => setProgress(p => [...p, t("将在当前步骤完成后停止")])).catch(report)}><Square size={16}/></button> : <button className="send-button" aria-label={t("发送问题")} disabled={!draft.trim() || busy || !!editingTurn || !modelReady || !scopeReady || !skillReady || !partnerReady || !connectorReady || !imageReady || uploadingImages}>{starting ? <LoaderCircle className="spin" size={19}/> : <ArrowUp size={21}/>}</button>}</div></div></form><p className="composer-note">{editingTurn ? t("请先完成或取消上方修改。") : !imageReady ? t("所选模型未启用图片输入，请更换模型或在设置中开启。") : !connectorReady ? t('所选连接器工具不可用，请连接服务或清空选择。') : connectorIds.length ? t('已授权连接器工具，助手可访问对应服务；外部操作需确认。') : !skillReady ? t("所选技能不可用或需要知识库，请调整选择后提问。") : !scopeReady ? t("所选知识库包含不可用项，请取消选择或完成索引后再提问。") : preferences?.web_tools && [preferences.web_tools.web_search,preferences.web_tools.web_fetch,preferences.web_tools.paper_search].some(Boolean) ? '' : kbIds.length ? t("仅检索所选知识库，点击引用可核对原文。历史对话仍会作为上下文。") : t("未选择知识库：使用模型知识和对话上下文，不检索资料或联网搜索。")}</p></div>
    </main>

    {sideOpen && <aside className="resource-panel"><div className="resource-heading"><h2>{t("我的空间")}</h2><span className="local-label">{t("本地保存")}</span></div><div className="tabs" role="tablist">{(['notes', 'artifacts'] as const).map((name, i) => <button role="tab" aria-selected={tab === name} className={tab === name ? 'active' : ''} key={name} onClick={() => setTab(name)}>{[t("笔记"), t("成果")][i]}</button>)}</div>
      <div className="resource-body">
        {tab === 'notes' && <><button className="outline-wide" onClick={() => { setNotePreview(false); setEditing({ title: '', content: '' }); }}><Plus size={16}/> {t("写一篇笔记")}</button><div className="section-label">{t("已保存 ·")}{notes.length}</div>{notes.map(n => <button key={n.id} className="note-row" onClick={() => { setNotePreview(false); setEditing(n); }}><BookMarked size={18}/><span><b>{n.title}</b><small>{n.content.replace(/\[\[c_[a-f0-9]{24}\]\]/g, '').replace(/[#*\[\]]/g, '').slice(0, 70)}</small></span><ChevronRight size={15}/></button>)}{!notes.length && <div className="empty-state"><BookMarked size={29}/><h3>{t("留下一点自己的理解")}</h3><p>{t("回答下方的“保存为笔记”，")}<br/>{t("可以把有价值的内容留下来。")}</p></div>}</>}
        {tab === 'artifacts' && <><div className="section-label">{t("当前会话的成果 ·")}{artifacts.length}</div>{artifacts.map(name => <button key={name} className="note-row" onClick={() => api<{ title: string; content: string }>('/sessions/' + tid + '/artifacts/' + encodeURIComponent(name)).then(a => { if(name.endsWith('.html'))setArtifactPreview(a);else{setEditing(a);setNotePreview(true);} }).catch(report)}><FileText size={18}/><span><b>{name}</b><small>{name.endsWith('.html')?t("可交互 HTML 成果"):t("模型生成的本地文件")}</small></span><ChevronRight size={15}/></button>)}{!artifacts.length && <div className="empty-state"><FolderOpen size={30}/><h3>{t("成果会出现在这里")}</h3><p>{t("试着让工作台把总结保存成文件，")}<br/>{t("然后在这里查看和导出。")}</p></div>}</>}
      </div><div className="resource-footer"><Leaf size={15}/><span>{t("记录思考，沉淀成果。")}</span></div></aside>}

    {notice && <div role="status" className="toast"><Check size={17}/>{notice}</div>}
    </>}
    {source && <div className="modal-backdrop" onClick={() => setSource(null)}><section className="modal source-modal" role="dialog" aria-modal="true" aria-label={t("引用原文")} onClick={e => e.stopPropagation()}><header><div><span className="eyebrow">{t("SOURCE · 原文依据")}</span><h2>{source[0]?.title}</h2>{source[0]?.kb_name && <p>{t("知识库：{0}", source[0].kb_name)}</p>}</div><button className="icon-button" aria-label={t("关闭原文")} onClick={() => setSource(null)}><X/></button></header><div className="modal-scroll">{source[0]?.archived && <div className="archive-banner">{t("这是回答当时使用的历史版本，已更新或移出当前资料库。")}</div>}{source.map(c => <section className="source-chunk" key={c.id}><div className="source-meta">{c.external ? <a href={c.url} target="_blank" rel="noreferrer">{c.kind==='paper_search'?t("论文摘要来源"):t("网页来源")}</a> : c.page ? t("第 {0} 页 · ", c.page) : ''}{c.external ? <> · {c.retrieved_at}</> : c.rag ? t("片段 {0}", c.paragraph) : t("第 {0} 段 · 行 {1}–{2}", c.paragraph, c.start_line, c.end_line)}<span>{t("版本")}{c.version.slice(0, 8)}</span></div><pre>{c.text}</pre></section>)}</div><footer>{source[0]?.external ? t("这是回答时获取的网络内容快照；搜索摘要与网页全文分别保留，原站内容可能变化。") : t("引用打开的是提取后的原文快照；PDF 的页码对应原始文件。")}</footer></section></div>}
    {editing && <div className="modal-backdrop"><section className="modal note-modal" role="dialog" aria-modal="true" aria-label={t("编辑笔记")}><header><div><span className="eyebrow">NOTEBOOK</span><h2>{editing.id ? t("编辑笔记") : t("留下这份思考")}</h2></div><button className="icon-button" aria-label={t("关闭笔记")} onClick={() => setEditing(null)}><X/></button></header><div className="note-form"><label>{t("标题")}<input autoFocus value={editing.title || ''} maxLength={120} placeholder={t("为笔记起个名字")} onChange={e => setEditing({ ...editing, title: e.target.value })}/></label><div className="editor-toolbar"><span>{t("Markdown 笔记")}</span><button onClick={() => setNotePreview(!notePreview)}>{notePreview ? t("编辑内容") : t("预览排版")}</button></div>{notePreview ? <div className="note-preview markdown">{markdown(editing.content || '')}</div> : <textarea aria-label={t("笔记内容")} value={editing.content || ''} maxLength={32000} placeholder={t("记录自己的理解，也可以保留回答中的引用…")} onChange={e => setEditing({ ...editing, content: e.target.value })}/>}</div><footer className="button-row"><button className="secondary" onClick={() => download(editing.title || t("笔记"), editing.content || '')}><Download size={15}/> {t("导出 Markdown")}</button><button className="primary" disabled={saving || !editing.title?.trim() || !editing.content?.trim()} onClick={saveNote}>{saving ? t("正在保存…") : t("保存笔记")}</button></footer></section></div>}
    {artifactPreview&&<div className="modal-backdrop"><section className="modal artifact-modal" role="dialog" aria-modal="true" aria-label={t("预览 HTML 成果")}><header><div><span className="eyebrow">HTML ARTIFACT</span><h2>{artifactPreview.title}</h2></div><button className="icon-button" aria-label={t("关闭预览")} onClick={()=>setArtifactPreview(null)}><X/></button></header><iframe sandbox="allow-scripts" title={artifactPreview.title} srcDoc={artifactPreview.content}/><footer className="button-row"><button className="secondary" onClick={()=>downloadArtifact(artifactPreview.title,artifactPreview.content)}><Download size={15}/>{t("下载 HTML")}</button></footer></section></div>}
    {deleteAnswer && <div className="modal-backdrop"><section className="modal small-modal" role="dialog" aria-modal="true" aria-label={t("删除回答")}><h2>{t("删除这条回答？")}</h2><p>{t("回答会从对话中移除，这一轮提问与回答也不会再进入后续模型上下文。原始记录仍保留在本机内部，用于数据一致性与故障恢复。")}</p><div className="button-row"><button className="secondary" onClick={()=>setDeleteAnswer(null)}>{t("取消")}</button><button className="primary danger-action" disabled={starting} onClick={confirmDeleteAnswer}>{starting?t("正在删除…"):t("确认删除")}</button></div></section></div>}
    {creatingProject&&<div className="modal-backdrop"><form className="modal small-modal" onSubmit={createProject}><h2>{t('新建项目')}</h2><p>{t('项目用于集中整理相关对话。每段对话的模型、知识库和工具仍在对话中选择。')}</p><label>{t('项目名称')}<input autoFocus required maxLength={80} value={projectName} onChange={e=>setProjectName(e.target.value)} placeholder={t('例如：LangGraph 学习')}/></label><div className="button-row"><button type="button" className="secondary" onClick={()=>setCreatingProject(false)}>{t('取消')}</button><button className="primary" disabled={saving||!projectName.trim()}>{t('创建项目')}</button></div></form></div>}
  </div>;
}

createRoot(document.getElementById('root')!).render(<App/>);
