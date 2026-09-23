import {useEffect,useState} from 'react';
import {Plus,Pencil,Search,Trash2,X} from 'lucide-react';
import {api} from './api';
import {t} from './i18n';
type Revision={id:string;content:string;valid:boolean};
type Method={id:string;version:number;title:string;enabled:boolean;locked:boolean;state:string;active:Revision|null;candidate:Revision|null;previous:Revision|null};
type Data={items:Method[];more:boolean;pending:{id:string;state:string}[]};
type Editor={id?:string;version?:number;title:string;content:string};
export function Mem0Procedures({space,readOnly,refreshKey,onChanged}:{space:string;readOnly:boolean;refreshKey:unknown;onChanged:()=>void}){
 const base='/memory/mem0/'+space;
 const [data,setData]=useState<Data|null>(null),[query,setQuery]=useState(''),[page,setPage]=useState(1),[tick,setTick]=useState(0);
 const [editor,setEditor]=useState<Editor|null>(null),[confirm,setConfirm]=useState<{method:Method;action:string}|null>(null);
 const [busy,setBusy]=useState(false),[error,setError]=useState(''),[loadError,setLoadError]=useState(''),[notice,setNotice]=useState('');
 useEffect(()=>{let cancelled=false;api<Data>(`${base}/methods?q=${encodeURIComponent(query)}&page=${page}`).then(v=>{if(!cancelled){setData(v);setLoadError('')}}).catch(e=>{if(!cancelled){setData(null);setLoadError(e.message)}});const timer=setTimeout(()=>setTick(v=>v+1),5000);return()=>{cancelled=true;clearTimeout(timer)}},[base,query,page,tick,refreshKey]);
 async function run(fn:()=>Promise<void>){setBusy(true);setError('');setNotice('');try{await fn();setTick(v=>v+1);onChanged()}catch(e){setError(e instanceof Error?e.message:t('操作未完成。'))}finally{setBusy(false)}}
 async function act(m:Method,action:string){const result=await api<{saved:boolean}>(`${base}/methods/${m.id}/${action}`,{method:'POST',body:JSON.stringify({version:m.version})});setConfirm(null);setNotice(t(result.saved?'方法已更新。':'方法保存尚未完成，请核对保存结果。'))}
 const disabled=busy||readOnly;
 return <section className="mem0-procedures">
  <p className="field-hint">{t('把可复用的经验整理成方法。待确认内容不会用于回答，确认使用后才生效。')}</p>
  <div className="memory-toolbar lm-fact-toolbar"><div className="memory-search"><Search size={16}/><input className="memory-search-input" aria-label={t('搜索方法')} placeholder={t('搜索方法')} maxLength={200} value={query} onChange={e=>{setQuery(e.target.value);setPage(1)}}/>{query&&<button aria-label={t('清空搜索')} onClick={()=>setQuery('')}><X size={14}/></button>}</div><button className="secondary" disabled={disabled} onClick={()=>{setError('');setEditor({title:'',content:''})}}><Plus size={15}/>{t('添加方法')}</button></div>
  {(error||loadError)&&<p role="alert" className="knowledge-error">{error||loadError}</p>}{notice&&<p role="status" className="knowledge-notice">{notice}</p>}
  {data?.pending.map(op=><div className="knowledge-notice" key={op.id}><p>{t(op.state==='failed'?'方法整理未完成，原有方法保持不变。':'方法保存尚未完成，请核对保存结果。')}</p><button className="secondary" disabled={disabled} onClick={()=>run(async()=>{await api(base+'/procedure-operations/'+op.id+'/resolve',{method:'POST'})})}>{t(op.state==='failed'?'忽略此次失败':'核对保存结果')}</button></div>)}
  {!data&&!loadError&&<p role="status">{t('正在加载…')}</p>}
  {data&&!data.items.length&&!data.pending.length&&<div className="memory-card memory-empty-note"><p>{t('还没有可复用的方法。你可以手动添加，也可以在对话中明确说明希望记住的方法。')}</p></div>}
  <div className="mem0-method-grid">{data?.items.map(m=><article className="memory-card mem0-method-card" key={m.id}>
   <header className="lm-sheet-heading"><h3>{m.title}</h3><span className="mem0-method-status">{t(m.state==='deleting'?'正在删除':m.enabled&&m.active?.valid?'正在使用':m.active&&!m.active.valid?'依据已变化':m.active?'已停用':'待确认')}</span></header>
   {m.state==='deleting'?<button className="secondary" disabled={disabled} onClick={()=>{setError('');setConfirm({method:m,action:'delete'})}}>{t('继续删除')}</button>:<>
    {m.active&&<div className="mem0-method-section"><h4>{t('当前版本')}</h4><p className="memory-content">{m.active.content}</p>{!m.active.valid&&<p className="field-hint">{t('依据已被更改或移除，此版本暂不用于回答。')}</p>}</div>}
    {m.candidate&&<div className="mem0-method-section mem0-method-candidate"><h4>{t('待确认内容')}</h4><p className="memory-content">{m.candidate.content}</p>{!m.candidate.valid&&<p className="field-hint">{t('候选依据已失效，请移除候选后重新整理。')}</p>}</div>}
    <div className="button-row mem0-method-actions">
     {(m.candidate?.valid||(!m.enabled&&m.active?.valid))&&<button className="primary" disabled={disabled} onClick={()=>{setError('');setConfirm({method:m,action:'enable'})}}>{t(m.candidate&&m.active?'确认替换':'确认使用')}</button>}
     <button className="secondary" disabled={disabled||!!(m.candidate&&!m.candidate.valid)} onClick={()=>{setError('');setEditor({id:m.id,version:m.version,title:m.title,content:m.candidate?.content||m.active?.content||''})}}><Pencil size={14}/>{t(m.candidate?'编辑候选':'编辑')}</button>
     {!!m.enabled&&<button className="secondary" disabled={disabled} onClick={()=>run(()=>act(m,'disable'))}>{t('停用')}</button>}
     {m.previous?.valid&&<button className="secondary" disabled={disabled} onClick={()=>{setError('');setConfirm({method:m,action:'rollback'})}}>{t('恢复上一版')}</button>}
     {m.candidate&&<button className="secondary" disabled={disabled} onClick={()=>{setError('');setConfirm({method:m,action:'discard'})}}>{t('移除候选')}</button>}
     <button className="secondary" disabled={disabled} onClick={()=>{setError('');setConfirm({method:m,action:'delete'})}}><Trash2 size={14}/>{t('删除')}</button>
    </div><button className="mem0-method-maintenance" disabled={disabled} onClick={()=>run(()=>act(m,m.locked?'unlock':'lock'))}>{t(m.locked?'已保护 · 允许自动整理候选':'可自动整理候选 · 保护此方法')}</button>
   </>}
  </article>)}</div>
  {(page>1||data?.more)&&<div className="button-row"><button className="secondary" disabled={page<=1} onClick={()=>setPage(v=>v-1)}>{t('上一页')}</button><button className="secondary" disabled={!data?.more} onClick={()=>setPage(v=>v+1)}>{t('下一页')}</button></div>}
  {editor&&<div className="modal-backdrop"><section role="dialog" aria-modal="true" aria-labelledby="mem0-method-editor" className="modal small-modal lm-group-editor"><header><h2 id="mem0-method-editor">{t(editor.id?'编辑方法':'添加方法')}</h2><button disabled={busy} aria-label={t('关闭')} onClick={()=>setEditor(null)}><X size={18}/></button></header><form className="memory-edit-form" onSubmit={e=>{e.preventDefault();run(async()=>{const {id,...body}=editor;const result=await api<{status:string}>(base+'/methods'+(id?'/'+id:''),{method:id?'PUT':'POST',body:JSON.stringify(body)});setEditor(null);setNotice(t(result.status==='completed'?'候选方法已保存，请检查后确认使用。':'方法整理未完成，原有方法保持不变。'))})}}>
   <label>{t('方法名称')}<input required maxLength={100} value={editor.title} onChange={e=>setEditor({...editor,title:e.target.value})}/></label><label>{t(editor.id?'方法内容':'方法依据')}<textarea required rows={9} minLength={2} maxLength={5000} value={editor.content} onChange={e=>setEditor({...editor,content:e.target.value})}/></label>
   <p className="field-hint">{t(editor.id?'修改仅保存为候选，当前使用的版本保持不变。手动编辑后默认保护此方法。':'请写明适用情形和具体步骤。Mem0 整理后先保存为候选，需要你确认才用于回答。')}</p>
   {error&&<p role="alert" className="knowledge-error">{error}</p>}<div className="button-row"><button type="button" className="secondary" disabled={busy} onClick={()=>setEditor(null)}>{t('取消')}</button><button className="primary" disabled={disabled}>{t(busy?'正在保存…':editor.id?'保存候选':'整理为候选')}</button></div>
  </form></section></div>}
  {confirm&&<div className="modal-backdrop"><section role="dialog" aria-modal="true" aria-labelledby="mem0-method-confirm" className="modal small-modal lm-group-editor"><header><h2 id="mem0-method-confirm">{t(confirm.action==='delete'?'删除方法':confirm.action==='discard'?'移除候选':confirm.action==='rollback'?'恢复上一版':'确认使用方法')}</h2><button disabled={busy} aria-label={t('关闭')} onClick={()=>setConfirm(null)}><X size={18}/></button></header><h3>{confirm.method.title}</h3>
   {['enable','rollback'].includes(confirm.action)?<><div className="mem0-method-section"><h4>{t('当前版本')}</h4><p className="memory-content">{confirm.method.active?.content||t('尚未启用')}</p></div><div className="mem0-method-section mem0-method-candidate"><h4>{t('即将使用的内容')}</h4><p className="memory-content">{confirm.action==='rollback'?confirm.method.previous?.content:confirm.method.candidate?.content||confirm.method.active?.content}</p></div><p className="field-hint">{t('确认后作为相关问题的方法参考，不改变工具权限或本次明确要求。')}</p></>:<p>{t(confirm.action==='discard'?'移除这份候选；当前已启用版本保持不变。原始聊天和已有备份保留。':'删除整个方法及其修订，停止在回答中使用。原始聊天和已有备份保留。')}</p>}
   {error&&<p role="alert" className="knowledge-error">{error}</p>}<div className="button-row"><button className="secondary" disabled={busy} onClick={()=>setConfirm(null)}>{t('取消')}</button><button className="primary" disabled={disabled} onClick={()=>run(()=>act(confirm.method,confirm.action))}>{t(['delete','discard'].includes(confirm.action)?'确认删除':'确认使用')}</button></div>
  </section></div>}
 </section>
}
