import { useEffect, useState } from 'react';
import { CircleUserRound, Clock3, ListChecks, Pencil, Search, X } from 'lucide-react';
import { api } from './api';
import {useMem0Procedures} from './useMem0Procedures';
import { Mem0Procedures } from './Mem0Procedures';
import { Mem0Events } from './Mem0Events';
import { t } from './i18n';

type Group={key:string;label:string;page:number;more:boolean;items:{id:string;content:string}[]};
type Groups={ready:boolean;pending:{id:string;category:string;state:string}|null;groups:Group[]};
type Editor={category:string;label:string;token:string;content:string;protection:string};
type Preview={confirmation:string;removed:string[];context_reset:boolean};
export function Mem0Workspace({space,readOnly,refreshKey,onChanged}:{space:string;readOnly:boolean;refreshKey:unknown;onChanged:()=>void;onSource:(id:string)=>void}){
 const proceduresReady=useMem0Procedures();
 const [layer,setLayer]=useState<'ordinary'|'event'|'procedure'>('ordinary');
 const base='/memory/mem0/'+space;
 const [data,setData]=useState<Groups|null>(null),[query,setQuery]=useState(''),[pages,setPages]=useState<Record<string,number>>({});
 const [loadError,setLoadError]=useState('');
 const [error,setError]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false),[tick,setTick]=useState(0);
 const [editor,setEditor]=useState<Editor|null>(null),[preview,setPreview]=useState<Preview|null>(null);
 const pageQuery=JSON.stringify(pages),disabled=busy||readOnly;
 const report=(e:unknown)=>setError(e instanceof Error?e.message:t('操作未完成。'));
 useEffect(()=>{
  let cancelled=false;
  api<Groups>(base+`/groups?q=${encodeURIComponent(query)}&pages=${encodeURIComponent(pageQuery)}`).then(v=>{if(!cancelled){setData(v);setLoadError('')}}).catch(e=>{if(!cancelled){setData(null);setLoadError(e instanceof Error?e.message:t('操作未完成。'))}});
  const timer=setTimeout(()=>setTick(v=>v+1),5000);
  return()=>{cancelled=true;clearTimeout(timer)};
 },[base,query,pageQuery,tick,refreshKey]);
 async function run(fn:()=>Promise<void>){setBusy(true);setError('');setNotice('');try{await fn()}catch(e){report(e)}finally{setBusy(false)}}
 function changed(){setTick(v=>v+1);onChanged()}
 async function save(){
  if(!editor)return;
  const url=base+'/categories/'+editor.category;
  const body={token:editor.token,content:editor.content,locked:editor.protection==='keep'?null:editor.protection==='lock'};
  if(!preview){
   const p=await api<Preview>(url+'/preview',{method:'POST',body:JSON.stringify(body)});
   if(p.removed.length){setPreview(p);return}
  }
  const result=await api<{saved:boolean}>(url,{method:'PUT',body:JSON.stringify({...body,confirmation:preview?.confirmation})});
  setEditor(null);setPreview(null);setPages({});
  setNotice(t(result.saved?'记忆已保存。':'保存尚未完成。当前空间暂停使用记忆，请重试完成保存。'));
  changed();
 }
 return <section className="mem0-workspace">
  <div className="memory-layer-tabs" role="tablist" aria-label={t('记忆分层')}>
   <button role="tab" aria-selected={layer==='ordinary'} onClick={()=>setLayer('ordinary')}><span className="memory-layer-icon"><CircleUserRound size={18}/></span><span><b>{t('个人信息与偏好')}</b><small>{t('稳定事实、偏好与目标')}</small></span></button>
   <button role="tab" aria-selected={layer==='event'} onClick={()=>setLayer('event')}><span className="memory-layer-icon"><Clock3 size={18}/></span><span><b>{t('事件与经历')}</b><small>{t('重要进展与实际结果')}</small></span></button>
   {proceduresReady&&<button role="tab" aria-selected={layer==='procedure'} onClick={()=>setLayer('procedure')}><span className="memory-layer-icon"><ListChecks size={18}/></span><span><b>{t('方法与流程')}</b><small>{t('已启用和待确认的方法')}</small></span></button>}
  </div>
  {layer==='procedure'?<Mem0Procedures space={space} readOnly={readOnly} refreshKey={refreshKey} onChanged={onChanged}/>:layer==='event'?<Mem0Events space={space} readOnly={readOnly} refreshKey={refreshKey} onChanged={onChanged}/>:<>
  <p className="field-hint">{t('按类型查看和管理记忆。没有分类的已有记忆保留在“其他信息”。')}</p>
  {loadError&&<div role="alert" className="knowledge-error">{loadError}</div>}
  {error&&<div role="alert" className="knowledge-error">{error}</div>}
  {notice&&<div role="status" className="knowledge-notice">{notice}</div>}
  {!data&&!loadError&&<p role="status">{t('正在加载…')}</p>}
  {data&&!data.ready&&<p>{t('此空间的记忆存储尚未就绪，请在记忆引擎中检查配置。')}</p>}
  {data?.pending&&<div className="knowledge-notice" role="status"><p>{t('保存尚未完成。当前空间暂停使用记忆，请重试完成保存。')}</p><button className="secondary" disabled={disabled} onClick={()=>run(async()=>{
   const result=await api<{saved:boolean}>(base+'/category-batches/'+data.pending!.id+'/retry',{method:'POST'});
   setNotice(t(result.saved?'记忆已保存。':'保存仍未完成，请检查记忆引擎配置后重试。'));changed();
  })}>{t('重试保存')}</button></div>}
  {data?.ready&&!data.pending&&<>
   <div className="memory-toolbar lm-fact-toolbar"><div className="memory-search"><Search size={16}/><input className="memory-search-input" aria-label={t('搜索记忆')} placeholder={t('搜索记忆')} maxLength={200} value={query} onChange={e=>{setQuery(e.target.value);setPages({})}}/>{query&&<button aria-label={t('清空搜索')} onClick={()=>{setQuery('');setPages({})}}><X size={14}/></button>}</div></div>
   <div className="lm-grouped-facts">{data.groups.map(group=><article className="memory-card lm-fact-group" key={group.key}>
    <header className="lm-sheet-heading"><h3>{t(group.label)}</h3><button className="secondary" disabled={disabled} onClick={()=>run(async()=>{
     const value=await api<{token:string;content:string}>(base+'/categories/'+group.key);
     setPreview(null);setEditor({category:group.key,label:group.label,...value,protection:'keep'});
    })}><Pencil size={14}/>{t('管理')}</button></header>
    {!group.items.length?<p className="field-hint">{t('此类型暂无匹配的记忆。')}</p>:<>{group.items.slice(0,3).map(item=><div className="lm-fact-row lm-fact-text" key={item.id}><p className="memory-content">{item.content}</p></div>)}{(group.more||group.items.length>3)&&<p className="lm-preview-note">{t('仅显示部分内容。点击“管理”查看全部内容。')}</p>}</>}
   </article>)}</div>
  </>}
  {editor&&<div className="modal-backdrop"><section role="dialog" aria-modal="true" aria-labelledby="mem0-group-title" className="modal small-modal lm-group-editor">
   <header><h2 id="mem0-group-title">{t(preview?'确认删除并保存':'管理')} · {t(editor.label)}</h2><button aria-label={t('关闭')} disabled={busy} onClick={()=>{setEditor(null);setPreview(null)}}><X size={18}/></button></header>
   <form className="memory-edit-form" onSubmit={e=>{e.preventDefault();run(save)}}>
    {preview?<><p>{t('以下记忆将被删除，确认后与本次编辑一起保存。')}</p><ul>{preview.removed.map((text,index)=><li key={index}>{text}</li>)}</ul><p className="field-hint">{t('相关会话摘要会按来源失效重建。原始聊天和已有备份保留。')}</p></>:<>
     <label>{t('记忆内容')}<textarea rows={10} maxLength={200000} value={editor.content} onChange={e=>setEditor({...editor,content:e.target.value})}/></label>
     <p className="field-hint">{t('每行一条记忆；这里编辑此类型的全部内容，不受搜索或分页影响。')}</p>
     <label>{t('自动维护')}<select value={editor.protection} onChange={e=>setEditor({...editor,protection:e.target.value})}><option value="keep">{t('保留原设置，新增和修改内容自动锁定')}</option><option value="lock">{t('锁定此类型的全部记忆')}</option><option value="unlock">{t('允许自动维护此类型的全部记忆')}</option></select></label>
    </>}
    {error&&<p role="alert" className="knowledge-error">{error}</p>}
    <div className="button-row"><button type="button" className="secondary" disabled={busy} onClick={()=>preview?setPreview(null):setEditor(null)}>{t(preview?'返回编辑':'取消')}</button><button className="primary" disabled={disabled}>{t(busy?'正在保存…':preview?'确认删除并保存':'保存')}</button></div>
   </form>
  </section></div>}
 </>}
 </section>
}
