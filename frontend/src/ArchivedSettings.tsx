import {useEffect,useState} from 'react';
import {Archive,Folder,MessageSquare,RotateCcw,Trash2} from 'lucide-react';
import {api} from './api';
import {t} from './i18n';
import type {Project,SessionPage,SessionSummary} from './conversations';

type Target={kind:'project'|'conversation';id:string;name:string};

export function ArchivedSettings({onChanged}:{onChanged:()=>Promise<void>}){
  const [kind,setKind]=useState<'projects'|'conversations'>('projects');
  const [projects,setProjects]=useState<Project[]>([]);
  const [conversations,setConversations]=useState<SessionSummary[]>([]);
  const [conversationTotal,setConversationTotal]=useState(0);
  const [conversationHasMore,setConversationHasMore]=useState(false);
  const [loading,setLoading]=useState(true);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [notice,setNotice]=useState('');
  const [deleting,setDeleting]=useState<Target|null>(null);

  async function load(){
    const [projectItems,sessionPage]=await Promise.all([
      api<Project[]>('/projects?archived=true'),
      api<SessionPage>('/sessions?paged=true&archived=true&offset=0&limit=100'),
    ]);
    setProjects(projectItems);setConversations(sessionPage.items);setConversationTotal(sessionPage.total);setConversationHasMore(sessionPage.has_more);
  }
  async function loadMore(){setBusy(true);setError('');try{const page=await api<SessionPage>('/sessions?paged=true&archived=true&offset='+conversations.length+'&limit=100');setConversations(items=>[...items,...page.items]);setConversationTotal(page.total);setConversationHasMore(page.has_more)}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  useEffect(()=>{load().catch(e=>setError(e.message)).finally(()=>setLoading(false))},[]);
  async function act(action:()=>Promise<unknown>,message:string){
    setBusy(true);setError('');setNotice('');
    try{await action();await load();await onChanged();setNotice(message);return true}catch(e){setError((e as Error).message);return false}finally{setBusy(false)}
  }
  const items=kind==='projects'?projects:conversations;
  return <section className="settings-models settings-archive">
    <div className="settings-archive-heading"><div><h2><Archive size={20}/>{t('已归档')}</h2><p>{t('在这里恢复或永久删除已归档的项目和对话。')}</p></div><span>{projects.length+conversationTotal}</span></div>
    <div className="knowledge-tabs" role="tablist" aria-label={t('已归档')}>
      <button role="tab" aria-selected={kind==='projects'} onClick={()=>setKind('projects')}>{t('已归档项目')}<small>{projects.length}</small></button>
      <button role="tab" aria-selected={kind==='conversations'} onClick={()=>setKind('conversations')}>{t('归档对话')}<small>{conversationTotal}</small></button>
    </div>
    {error&&<div className="knowledge-error" role="alert">{error}</div>}{notice&&<div className="knowledge-notice" role="status">{notice}</div>}
    {loading?<p className="settings-archive-empty">{t('正在加载…')}</p>:!items.length?<div className="settings-archive-empty"><Archive size={25}/><p>{t(kind==='projects'?'还没有归档项目':'还没有归档对话')}</p></div>:<div className="settings-archive-list">
      {kind==='projects'?projects.map(project=><article key={project.id}><span className="settings-archive-icon"><Folder size={17}/></span><div><b>{project.name}</b><small>{t('{0} 段对话',project.conversation_count)}</small></div><button className="secondary" disabled={busy} onClick={()=>act(()=>api('/projects/'+project.id+'/restore',{method:'POST'}),t('项目已恢复。'))}><RotateCcw size={14}/>{t('恢复')}</button><button className="settings-delete" disabled={busy} onClick={()=>setDeleting({kind:'project',id:project.id,name:project.name})}><Trash2 size={14}/>{t('删除')}</button></article>):conversations.map(session=><article key={session.id}><span className="settings-archive-icon"><MessageSquare size={17}/></span><div><b>{session.title}</b><small>{new Date(session.updated).toLocaleString()}</small></div><button className="secondary" disabled={busy} onClick={()=>act(()=>api('/sessions/'+session.id+'/restore',{method:'POST'}),t('对话已恢复。'))}><RotateCcw size={14}/>{t('恢复')}</button><button className="settings-delete" disabled={busy} onClick={()=>setDeleting({kind:'conversation',id:session.id,name:session.title})}><Trash2 size={14}/>{t('删除')}</button></article>)}
      {kind==='conversations'&&conversationHasMore&&<button className="load-more" disabled={busy} onClick={loadMore}>{t('加载更多')}</button>}
    </div>}
    {deleting&&<div className="modal-backdrop"><section className="modal small-modal" role="dialog" aria-label={t('确认永久删除')}><h2>{t('确认永久删除')}</h2><p>「{deleting.name}」</p><p>{deleting.kind==='project'?t('项目内的对话会移动到未归类对话，不会被删除。'):t('聊天、上下文和运行记录会永久删除。已保存笔记和长期记忆保留。')}</p><div className="button-row"><button className="secondary" disabled={busy} onClick={()=>setDeleting(null)}>{t('取消')}</button><button className="danger-button" disabled={busy} onClick={async()=>{if(await act(()=>api(deleting.kind==='project'?'/projects/'+deleting.id:'/sessions/'+deleting.id+'?delete_artifacts=false',{method:'DELETE'}),t(deleting.kind==='project'?'项目已删除。':'对话已永久删除。')))setDeleting(null)}}>{t('确认删除')}</button></div></section></div>}
  </section>;
}
