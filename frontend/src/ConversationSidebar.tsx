import {type MouseEvent as ReactMouseEvent,useEffect,useRef,useState} from 'react';
import {Archive,ChevronRight,CircleAlert,CircleCheck,CirclePause,CircleStop,CircleX,Clock3,Ellipsis,Folder,FolderInput,LoaderCircle,MessageSquare,Pencil,Pin,PinOff,Search,SquarePen,Trash2,X} from 'lucide-react';
import {api} from './api';
import {t} from './i18n';
import type {Project,SessionPage,SessionSummary} from './conversations';

type Props={
  sessions:SessionSummary[];projects:Project[];selectedId:string|null;selectedProjectId:string|null;
  total:number;hasMore:boolean;busy:boolean;query:string;statusBySession:Record<string,string>;
  onSelect:(id:string|null)=>void;onNewInProject:(id:string)=>void;onProjectUnavailable:(id:string)=>void;
  onChanged:()=>Promise<void>;onMore:()=>void;
  onCreateProject:()=>void;onSearch:(query:string)=>void;
};

type ProjectSessions=SessionPage&{loading?:boolean;error?:string};

export function ConversationSidebar(props:Props){
  const {sessions,projects,selectedId,selectedProjectId,total,hasMore,busy}=props;
  const [menu,setMenu]=useState<string|null>(null),[projectMenu,setProjectMenu]=useState<string|null>(null);
  const [rename,setRename]=useState<SessionSummary|null>(null),[renamingProject,setRenamingProject]=useState<Project|null>(null);
  const [moving,setMoving]=useState<SessionSummary|null>(null),[deleting,setDeleting]=useState<SessionSummary|null>(null);
  const [archivingProject,setArchivingProject]=useState<Project|null>(null),[deletingProject,setDeletingProject]=useState<Project|null>(null);
  const [sessionMenuPosition,setSessionMenuPosition]=useState<{top:number;left:number}|null>(null);
  const [projectMenuPosition,setProjectMenuPosition]=useState<{top:number;left:number}|null>(null);
  const [expanded,setExpanded]=useState<string[]>([]),[projectSessions,setProjectSessions]=useState<Record<string,ProjectSessions>>({});
  const [title,setTitle]=useState(''),[projectName,setProjectName]=useState(''),[deleteArtifacts,setDeleteArtifacts]=useState(false);
  const [error,setError]=useState(''),[saving,setSaving]=useState(false),[query,setQuery]=useState(props.query);
  const menuRef=useRef<HTMLDivElement|null>(null);

  useEffect(()=>{const close=(event:MouseEvent)=>{if(menuRef.current&&!menuRef.current.contains(event.target as Node)){setMenu(null);setSessionMenuPosition(null);setProjectMenu(null);setProjectMenuPosition(null)}};document.addEventListener('mousedown',close);return()=>document.removeEventListener('mousedown',close)},[]);
  useEffect(()=>setQuery(props.query),[props.query]);
  async function loadProject(id:string,append=false){
    const current=projectSessions[id];
    setProjectSessions(values=>({...values,[id]:{...(values[id]||{items:[],total:0,offset:0,limit:30,has_more:false}),loading:true,error:''}}));
    try{
      const offset=append?(current?.items.length||0):0;
      const page=await api<SessionPage>('/sessions?paged=true&archived=false&project_id='+id+'&offset='+offset+'&limit=30');
      setProjectSessions(values=>({...values,[id]:{...page,items:append?[...(values[id]?.items||[]),...page.items]:page.items,loading:false}}));
    }catch(e){setProjectSessions(values=>({...values,[id]:{...(values[id]||{items:[],total:0,offset:0,limit:30,has_more:false}),loading:false,error:(e as Error).message}}));throw e}
  }
  async function refreshExpanded(removedProjectId?:string){await Promise.all(expanded.filter(id=>id!==removedProjectId).map(id=>loadProject(id).catch(()=>undefined)))}
  async function run(action:()=>Promise<unknown>,after?:()=>void,removedProjectId?:string){setSaving(true);setError('');try{await action();after?.();setMenu(null);setSessionMenuPosition(null);setProjectMenu(null);setProjectMenuPosition(null);await props.onChanged();await refreshExpanded(removedProjectId)}catch(e){setError((e as Error).message)}finally{setSaving(false)}}
  function toggleProject(id:string){if(expanded.includes(id)){setExpanded(values=>values.filter(value=>value!==id));setProjectMenu(null);return}setExpanded(values=>[...values,id]);if(!projectSessions[id])loadProject(id).catch(e=>setError(e.message))}
  function newInProject(id:string){setProjectMenu(null);props.onNewInProject(id)}
  function toggleProjectMenu(event:ReactMouseEvent<HTMLButtonElement>,id:string){
    if(projectMenu===id){setProjectMenu(null);setProjectMenuPosition(null);return}
    const rect=event.currentTarget.getBoundingClientRect();
    setMenu(null);setSessionMenuPosition(null);setProjectMenu(id);setProjectMenuPosition({top:Math.min(rect.bottom+4,window.innerHeight-184),left:Math.max(8,rect.right-175)});
  }
  function toggleSessionMenu(event:ReactMouseEvent<HTMLButtonElement>,id:string){
    if(menu===id){setMenu(null);setSessionMenuPosition(null);return}
    const rect=event.currentTarget.getBoundingClientRect(),menuHeight=224;
    const below=rect.bottom+4;
    setProjectMenu(null);setProjectMenuPosition(null);setMenu(id);
    setSessionMenuPosition({top:below+menuHeight<=window.innerHeight-8?below:Math.max(8,rect.top-menuHeight-4),left:Math.max(8,rect.right-175)});
  }

  function stateIcon(status:string){
    const labels:Record<string,string>={queued:t('排队中'),running:t('进行中'),stopping:t('正在停止'),waiting_approval:t('待确认'),completed:t('已完成'),failed:t('未完成'),stopped:t('已停止'),interrupted:t('可继续'),limited:t('已达本轮上限'),rejected:t('已取消操作'),conflict:t('操作需核对'),created:t('已创建')};
    const label=labels[status];if(!label)return null;
    const icon=status==='running'?<LoaderCircle className="spin"/>:status==='queued'||status==='created'?<Clock3/>:status==='waiting_approval'||status==='limited'||status==='conflict'?<CircleAlert/>:status==='completed'?<CircleCheck/>:status==='failed'||status==='rejected'?<CircleX/>:status==='stopped'||status==='stopping'?<CircleStop/>:<CirclePause/>;
    return <span className={'session-status state-'+status} role="img" aria-label={label} title={label}>{icon}</span>;
  }
  const row=(session:SessionSummary,nested=false)=>{const status=props.statusBySession[session.id]||session.status;return <div className={'session-row '+(nested?'project-session-row ':'')+(selectedId===session.id?'selected':'')} key={session.id}>
    <button className="session-main" title={session.title} onClick={()=>props.onSelect(session.id)}><MessageSquare size={nested?13:15}/><span className="session-title">{session.title}</span>{session.pinned&&nested&&<Pin size={10}/>} {stateIcon(status)}</button>
    <button className="session-more" aria-label={t('管理对话')} aria-expanded={menu===session.id} onClick={event=>toggleSessionMenu(event,session.id)}><Ellipsis size={16}/></button>
  </div>};

  const pinned=!query?sessions.filter(s=>s.pinned):[];
  const regular=query?sessions:sessions.filter(s=>!s.pinned);
  const menuSession=menu?(sessions.find(session=>session.id===menu)||Object.values(projectSessions).flatMap(page=>page.items).find(session=>session.id===menu)):undefined;
  return <div className="conversation-nav" onScrollCapture={()=>{setMenu(null);setSessionMenuPosition(null);setProjectMenu(null);setProjectMenuPosition(null)}}>
    <form className="conversation-search" onSubmit={e=>{e.preventDefault();props.onSearch(query)}}><Search size={14}/><input aria-label={t('搜索对话')} placeholder={t('搜索对话')} value={query} onChange={e=>setQuery(e.target.value)}/>{query&&<button type="button" aria-label={t('清空搜索')} onClick={()=>{setQuery('');props.onSearch('')}}><X size={13}/></button>}</form>
    {!query&&<>
      {!!pinned.length&&<><div className="nav-label">{t('置顶')}<span>{pinned.length}</span></div><nav className="session-list pinned-session-list">{pinned.map(session=>row(session))}</nav></>}
      <div className="nav-label project-nav-label">{t('项目')}<button aria-label={t('新建项目')} onClick={props.onCreateProject}>＋</button></div>
      <nav className="project-nav">{projects.map(project=>{const open=expanded.includes(project.id),page=projectSessions[project.id];return <div className={'project-tree-item '+(open?'open ':'')+(selectedProjectId===project.id?'current':'')} key={project.id}>
        <div className="project-row">
          <button className="project-toggle" aria-expanded={open} onClick={()=>toggleProject(project.id)}><ChevronRight size={13}/><Folder size={15}/><span>{project.name}</span>{project.pinned&&<Pin size={10}/>}<small>{project.conversation_count}</small></button>
          <button className="project-row-action" aria-label={t('管理项目')} aria-expanded={projectMenu===project.id} title={t('管理项目')} onClick={event=>toggleProjectMenu(event,project.id)}><Ellipsis size={16}/></button>
          <button className="project-row-action project-new-chat" aria-label={t('在“{0}”中新建对话',project.name)} title={t('新建对话')} onClick={()=>newInProject(project.id)}><SquarePen size={16}/></button>
        </div>
        {open&&<div className="project-session-list">{page?.loading&&!page.items.length?<p className="project-loading"><LoaderCircle className="spin" size={13}/>{t('正在加载对话…')}</p>:page?.error?<p className="sidebar-error">{page.error}</p>:page?.items.length?page.items.map(session=>row(session,true)):<p className="project-empty-small">{t('此项目还没有对话')}</p>}{page?.has_more&&<button className="load-more" onClick={()=>loadProject(project.id,true).catch(e=>setError(e.message))}>{t('加载更多')}</button>}</div>}
      </div>})}</nav>
    </>}
    <div className="nav-label"><span>{query?t('搜索结果'):t('最近的对话')}</span><span>{total}</span></div>
    <nav className="session-list recent-session-list">{regular.map(session=>row(session))}{!regular.length&&<p className="empty-small">{t(query?'没有匹配的对话':'第一段对话，会从这里开始。')}</p>}{hasMore&&<button className="load-more" onClick={props.onMore}>{t('加载更多')}</button>}</nav>
    {error&&<p className="sidebar-error" role="alert">{error}</p>}
    {menuSession&&sessionMenuPosition&&<div className="session-menu conversation-floating-menu" ref={menuRef} style={sessionMenuPosition}>
      <button onClick={()=>{setRename(menuSession);setTitle(menuSession.title);setMenu(null);setSessionMenuPosition(null)}}><Pencil size={15}/>{t('重命名对话')}</button>
      <button disabled={saving} onClick={()=>run(()=>api('/sessions/'+menuSession.id,{method:'PATCH',body:JSON.stringify({pinned:!menuSession.pinned})}))}>{menuSession.pinned?<PinOff size={15}/>:<Pin size={15}/>} {t(menuSession.pinned?'取消置顶':'置顶')}</button>
      <button disabled={saving||busy} onClick={()=>{setMoving(menuSession);setMenu(null);setSessionMenuPosition(null)}}><FolderInput size={15}/>{t('移动到项目')}</button>
      <button disabled={saving||busy} onClick={()=>run(()=>api('/sessions/'+menuSession.id+'/archive',{method:'POST'}),()=>{if(selectedId===menuSession.id)props.onSelect(null)})}><Archive size={15}/> {t('归档')}</button>
      <hr/><button className="danger" disabled={saving||busy} onClick={()=>{setDeleting(menuSession);setDeleteArtifacts(false);setMenu(null);setSessionMenuPosition(null)}}><Trash2 size={15}/>{t('删除对话')}</button>
    </div>}
    {projectMenu&&projectMenuPosition&&(()=>{const project=projects.find(item=>item.id===projectMenu);return project?<div className="session-menu project-floating-menu" ref={menuRef} style={projectMenuPosition}>
      <button onClick={()=>{setRenamingProject(project);setProjectName(project.name);setProjectMenu(null)}}><Pencil size={15}/>{t('重命名项目')}</button>
      <button disabled={saving} onClick={()=>run(()=>api('/projects/'+project.id,{method:'PATCH',body:JSON.stringify({pinned:!project.pinned})}))}>{project.pinned?<PinOff size={15}/>:<Pin size={15}/>} {t(project.pinned?'取消置顶':'置顶')}</button>
      <button onClick={()=>{setArchivingProject(project);setProjectMenu(null)}}><Archive size={15}/>{t('归档项目')}</button>
      <hr/><button className="danger" onClick={()=>{setDeletingProject(project);setProjectMenu(null)}}><Trash2 size={15}/>{t('删除项目')}</button>
    </div>:null})()}
    {rename&&<div className="modal-backdrop"><form className="modal small-modal" onSubmit={e=>{e.preventDefault();run(()=>api('/sessions/'+rename.id,{method:'PATCH',body:JSON.stringify({title})}),()=>setRename(null))}}><h2>{t('重命名对话')}</h2><input autoFocus required maxLength={120} value={title} onChange={e=>setTitle(e.target.value)}/><div className="button-row"><button type="button" className="secondary" onClick={()=>setRename(null)}>{t('取消')}</button><button className="primary" disabled={saving||!title.trim()}>{t('保存')}</button></div></form></div>}
    {renamingProject&&<div className="modal-backdrop"><form className="modal small-modal" onSubmit={e=>{e.preventDefault();run(()=>api('/projects/'+renamingProject.id,{method:'PATCH',body:JSON.stringify({name:projectName.trim()})}),()=>setRenamingProject(null))}}><h2>{t('重命名项目')}</h2><label>{t('项目名称')}<input autoFocus required maxLength={80} value={projectName} onChange={e=>setProjectName(e.target.value)}/></label><div className="button-row"><button type="button" className="secondary" onClick={()=>setRenamingProject(null)}>{t('取消')}</button><button className="primary" disabled={saving||!projectName.trim()}>{t('保存')}</button></div></form></div>}
    {moving&&<div className="modal-backdrop"><section className="modal small-modal"><h2>{t('移动到项目')}</h2><p>{t('只改变对话的组织位置，不会更换这段对话已经使用的模型、知识库或助手。')}</p><div className="project-move-list"><button onClick={()=>run(()=>api('/sessions/'+moving.id,{method:'PATCH',body:JSON.stringify({project_id:null})}),()=>{if(selectedId===moving.id)props.onSelect(moving.id);setMoving(null)})}><Folder size={15}/>{t('未归类对话')}</button>{projects.map(project=><button key={project.id} onClick={()=>run(()=>api('/sessions/'+moving.id,{method:'PATCH',body:JSON.stringify({project_id:project.id})}),()=>{if(selectedId===moving.id)props.onSelect(moving.id);setMoving(null)})}><Folder size={15}/>{project.name}</button>)}</div><button className="secondary" onClick={()=>setMoving(null)}>{t('取消')}</button></section></div>}
    {deleting&&<div className="modal-backdrop"><section className="modal small-modal"><h2>{t('永久删除对话？')}</h2><p>「{deleting.title}」</p><p>{t('聊天、上下文和运行记录会永久删除。已保存笔记和长期记忆保留。')}</p><label className="check-label"><input type="checkbox" checked={deleteArtifacts} onChange={e=>setDeleteArtifacts(e.target.checked)}/>{t('同时删除这段对话生成的成果文件')}</label><div className="button-row"><button className="secondary" onClick={()=>setDeleting(null)}>{t('取消')}</button><button className="danger-button" disabled={saving} onClick={()=>run(()=>api('/sessions/'+deleting.id+'?delete_artifacts='+deleteArtifacts,{method:'DELETE'}),()=>{setDeleting(null);if(selectedId===deleting.id)props.onSelect(null)})}>{t('确认永久删除')}</button></div></section></div>}
    {archivingProject&&<div className="modal-backdrop"><section className="modal small-modal"><h2>{t('归档项目？')}</h2><p>{t('项目会从侧边栏隐藏，项目中的对话仍会保留。你可以稍后恢复项目。')}</p><div className="button-row"><button className="secondary" onClick={()=>setArchivingProject(null)}>{t('取消')}</button><button className="primary" disabled={saving} onClick={()=>run(()=>api('/projects/'+archivingProject.id+'/archive',{method:'POST'}),()=>{setExpanded(values=>values.filter(id=>id!==archivingProject.id));props.onProjectUnavailable(archivingProject.id);setArchivingProject(null)},archivingProject.id)}>{t('确认归档')}</button></div></section></div>}
    {deletingProject&&<div className="modal-backdrop"><section className="modal small-modal"><h2>{t('删除项目？')}</h2><p>{t('项目内的对话会移动到未归类对话，不会被删除。')}</p><div className="button-row"><button className="secondary" onClick={()=>setDeletingProject(null)}>{t('取消')}</button><button className="danger-button" disabled={saving} onClick={()=>run(()=>api('/projects/'+deletingProject.id,{method:'DELETE'}),()=>{setExpanded(values=>values.filter(id=>id!==deletingProject.id));props.onProjectUnavailable(deletingProject.id);setDeletingProject(null)},deletingProject.id)}>{t('确认删除项目')}</button></div></section></div>}
  </div>
}
