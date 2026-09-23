import { useEffect, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, LoaderCircle, Plug, Search, SlidersHorizontal, Wrench, X } from 'lucide-react';
import { api } from './api';
import { t, useLanguage } from './i18n';
import './tools.css';

type Tool = {id:string;name:string;description:string;source:'builtin'|'mcp';connector_id:string|null;connector_name:string;policy:string;status:string};
type Detail = Tool & {applicability:string;schema:Record<string,unknown>;read_hint?:boolean;idempotency_parameter?:string|null};
type Page = {items:Tool[];total:number;filtered_total:number;page:number;page_size:number;pages:number;connectors:{id:string;name:string;tool_count:number}[]};
type Filters = {q:string;source:string;policy:string;status:string;page:number;page_size:number};
const initial:Filters={q:'',source:'all',policy:'all',status:'all',page:1,page_size:20};
const policyLabels:Record<string,string>={contextual:'按场景使用',disabled:'停用',read:'只读',confirm:'每次确认'};
const statusLabels:Record<string,string>={contextual:'按场景可用',available:'可用',connected:'已连接',disconnected:'未连接'};
const label=(tool:Tool,value:string)=>tool.source==='builtin'?t(value):value;

export function ToolCatalog({onManage}:{onManage:(connectorId:string)=>void}) {
  useLanguage();
  const [query,setQuery]=useState('');
  const [filters,setFilters]=useState<Filters>(initial);
  const [data,setData]=useState<Page|null>(null);
  const [loading,setLoading]=useState(true);
  const [error,setError]=useState('');
  const [reload,setReload]=useState(0);
  const [selected,setSelected]=useState<Tool|null>(null);
  const [detail,setDetail]=useState<Detail|null>(null);
  const [detailError,setDetailError]=useState('');
  const [detailReload,setDetailReload]=useState(0);
  const dialog=useRef<HTMLDialogElement>(null);
  const listRoot=useRef<HTMLDivElement>(null);
  const opener=useRef<HTMLElement|null>(null);
  useEffect(()=>{const timer=setTimeout(()=>setFilters(f=>f.q===query.trim()?f:{...f,q:query.trim(),page:1}),200);return()=>clearTimeout(timer);},[query]);
  useEffect(()=>{
    const controller=new AbortController();setLoading(true);setError('');
    const params=new URLSearchParams({...Object.fromEntries(Object.entries(filters).map(([k,v])=>[k,String(v)]))});
    if(filters.source.startsWith('connector:')) {params.set('source','mcp');params.set('connector_id',filters.source.slice(10));}
    api<Page>('/tools?'+params,{signal:controller.signal}).then(result=>{if(!controller.signal.aborted)setData(result);}).catch(e=>{if(!controller.signal.aborted)setError(e.message);}).finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return()=>controller.abort();
  },[filters,reload]);
  useEffect(()=>{
    if(selected) {if(!dialog.current?.open){opener.current=document.activeElement as HTMLElement;dialog.current?.showModal();}}
    else {dialog.current?.close();opener.current?.focus();}
  },[selected]);
  useEffect(()=>{
    setDetail(null);setDetailError('');if(!selected)return;
    const controller=new AbortController();
    api<Detail>('/tools/'+encodeURIComponent(selected.id),{signal:controller.signal}).then(value=>{if(!controller.signal.aborted)setDetail(value);}).catch(e=>{if(!controller.signal.aborted)setDetailError(e.message);});
    return()=>controller.abort();
  },[selected,detailReload]);
  const update=(patch:Partial<Filters>)=>setFilters(f=>({...f,...patch,page:1}));
  const goToPage=(page:number)=>{setFilters(f=>({...f,page}));listRoot.current?.scrollIntoView({block:'start'});};
  const reset=()=>{setQuery('');setFilters(f=>({...initial,page_size:f.page_size}));};
  const filtered=!!query || filters.source!=='all' || filters.policy!=='all' || filters.status!=='all';
  const activeConnector=filters.source.startsWith('connector:')?filters.source.slice(10):null;
  const properties=detail?.schema.properties as Record<string,unknown>|undefined;
  const required=Array.isArray(detail?.schema.required)?detail.schema.required as string[]:[];
  return <section className="tool-directory" aria-label={t('工具目录')}>
    <div className="tool-directory-heading"><div className="skill-section-label"><h2>{t('工作台与连接器工具')}</h2><p>{t('内置工具按资料范围使用；连接器工具需明确授权与选择。')}</p></div><span className="tool-directory-count"><Wrench size={15}/>{t('共 {0} 个工具',data?.total ?? '—')}</span></div>
    <div className="tool-directory-filters"><label className="tool-search"><Search size={17}/><input type="search" aria-label={t('搜索工具')} placeholder={t('搜索名称、说明或连接器')} maxLength={200} value={query} onChange={e=>setQuery(e.target.value)}/></label><div className="tool-directory-selects"><SlidersHorizontal size={15} aria-hidden="true"/><label><span>{t('来源')}</span><select aria-label={t('工具来源')} value={filters.source} onChange={e=>update({source:e.target.value})}><option value="all">{t('全部来源')}</option><option value="builtin">{t('工作台内置')}</option><option value="mcp">{t('全部连接器')}</option>{data?.connectors.map(c=><option key={c.id} value={'connector:'+c.id}>{c.name} · {c.tool_count}</option>)}{activeConnector&&data&&!data.connectors.some(c=>c.id===activeConnector)&&<option value={filters.source}>{t('连接器已移除')}</option>}</select></label><label><span>{t('权限')}</span><select aria-label={t('调用权限筛选')} value={filters.policy} onChange={e=>update({policy:e.target.value})}><option value="all">{t('全部权限')}</option>{Object.entries(policyLabels).map(([value,name])=><option key={value} value={value}>{t(name)}</option>)}</select></label><label><span>{t('状态')}</span><select aria-label={t('可用状态筛选')} value={filters.status} onChange={e=>update({status:e.target.value})}><option value="all">{t('全部状态')}</option>{Object.entries(statusLabels).map(([value,name])=><option key={value} value={value}>{t(name)}</option>)}</select></label>{filtered&&<button className="skill-text-button" onClick={reset}><X size={13}/>{t('清除筛选')}</button>}</div></div>
    <div className="tool-results-meta" aria-live="polite"><span>{loading?t('正在加载工具…'):data?t('找到 {0} 个工具，共 {1} 个',data.filtered_total,data.total):''}</span><small>{t('已连接不等于已授权，实际调用仍需在对话中选择。')}</small></div>
    {error?<div className="skill-error" role="alert"><p>{error}</p><button className="secondary" onClick={()=>setReload(n=>n+1)}>{t('重新加载')}</button></div>:<div className="tool-table-wrap" ref={listRoot} aria-busy={loading}>
      <table className="tool-table"><caption className="sr-only">{t('工具目录')}</caption><thead><tr><th scope="col">{t('工具名称')}</th><th scope="col">{t('来源')}</th><th scope="col">{t('调用权限')}</th><th scope="col">{t('可用状态')}</th><th scope="col"><span className="sr-only">{t('查看详情')}</span></th></tr></thead><tbody>{data?.items.map(tool=><tr key={tool.id} className={selected?.id===tool.id?'selected':''} onClick={()=>setSelected(tool)}><td><button className="tool-row-open" aria-label={t('查看工具：{0}',label(tool,tool.name))}><span className="tool-row-icon">{tool.source==='builtin'?<Wrench size={17}/>:<Plug size={17}/>}</span><span><b>{label(tool,tool.name)}</b><small title={label(tool,tool.description)}>{label(tool,tool.description)}</small></span></button></td><td className="tool-row-source" title={tool.source==='builtin'?t('工作台内置'):tool.connector_name}>{tool.source==='builtin'?t('工作台内置'):tool.connector_name}</td><td><span className={'tool-policy tool-policy-'+tool.policy}>{t(policyLabels[tool.policy] || tool.policy)}</span></td><td><span className={'tool-availability tool-availability-'+tool.status}><i/>{t(statusLabels[tool.status] || tool.status)}</span></td><td><ChevronRight size={15} aria-hidden="true"/></td></tr>)}</tbody></table>
      {!data&&loading&&<div className="skill-loading"><LoaderCircle className="spin" size={19}/>{t('正在加载工具…')}</div>}
      {!loading&&data?.filtered_total===0&&<div className="tool-directory-empty"><Search size={25}/><h3>{t('没有找到匹配的工具')}</h3><p>{t('试试其他关键词，或清除筛选条件。')}</p>{filtered&&<button className="secondary" onClick={reset}>{t('清除筛选')}</button>}</div>}
    </div>}
    {data&&!error&&<nav className="tool-pagination" aria-label={t('工具分页')}><label>{t('每页')}<select aria-label={t('每页工具数量')} value={filters.page_size} onChange={e=>{update({page_size:Number(e.target.value)});listRoot.current?.scrollIntoView({block:'start'});}}><option value={20}>20</option><option value={50}>50</option></select>{t('个工具')}</label><div><span>{t('第 {0} / {1} 页',data.page,data.pages)}</span><button className="secondary" disabled={loading||data.page<=1} aria-label={t('上一页工具')} onClick={()=>goToPage(data.page-1)}><ChevronLeft size={15}/>{t('上一页')}</button><button className="secondary" disabled={loading||data.page>=data.pages} aria-label={t('下一页工具')} onClick={()=>goToPage(data.page+1)}>{t('下一页')}<ChevronRight size={15}/></button></div></nav>}
    <dialog ref={dialog} className="tool-detail-drawer" aria-labelledby="tool-drawer-title" onCancel={e=>{e.preventDefault();setSelected(null);}} onClick={e=>{if(e.target===e.currentTarget)setSelected(null);}} onClose={()=>setSelected(null)}>
      {selected&&<div className="tool-drawer-content"><header><span className="skill-badge">{t(selected.source==='builtin'?'内置':'外部工具')}</span><button className="icon-button" aria-label={t('关闭工具详情')} onClick={()=>setSelected(null)} autoFocus><X size={20}/></button></header><h2 id="tool-drawer-title">{label(selected,selected.name)}</h2><p className="tool-drawer-source">{selected.source==='builtin'?t('工作台内置'):selected.connector_name}</p>
      {detailError?<div className="skill-error" role="alert"><p>{detailError}</p><button className="secondary" onClick={()=>setDetailReload(n=>n+1)}>{t('重新加载')}</button></div>:!detail?<div className="skill-loading" role="status"><LoaderCircle className="spin" size={20}/>{t('正在加载详情…')}</div>:<><div className="tool-drawer-status"><span className={'tool-policy tool-policy-'+detail.policy}>{t(policyLabels[detail.policy] || detail.policy)}</span><span className={'tool-availability tool-availability-'+detail.status}><i/>{t(statusLabels[detail.status] || detail.status)}</span></div><p className="tool-full-description">{label(detail,detail.description)}</p><h3>{t('可用范围')}</h3><p>{t(detail.applicability)}</p>{detail.source==='mcp'&&<p className="skill-detail-hint">{t('连接器授权、伙伴范围与本轮选择共同决定实际可用工具。')}</p>}<h3>{t('工具参数')}</h3><p className="skill-detail-hint">{t('参数来自实际工具定义，由助手调用时填写。')}</p>{properties&&Object.keys(properties).length?<dl className="tool-schema-fields">{Object.entries(properties).map(([name,value])=>{const field=(typeof value==='object'&&value!==null?value:{}) as Record<string,unknown>;return <div key={name}><dt><code>{name}</code><span>{t(required.includes(name)?'必填':'可选')}</span></dt><dd><small>{Array.isArray(field.type)?field.type.join(' | '):typeof field.type==='string'?field.type:t('复合类型，见完整定义')}</small>{typeof field.description==='string'&&<p>{field.description}</p>}{field.default!==undefined&&<p>{t('默认值')}：<code>{JSON.stringify(field.default)}</code></p>}</dd></div>;})}</dl>:<p>{t('无需参数')}</p>}<details className="tool-schema-source"><summary>{t('查看完整参数定义')}</summary><pre>{JSON.stringify(detail.schema,null,2)}</pre></details><details className="tool-schema-source"><summary>{t('工具标识')}</summary><code>{detail.id}</code></details>{detail.id==='create_note'&&<p className="skill-detail-note">{t('这个工具只准备草稿。真正的文件写入由保存流程处理，覆盖已有文件仍需你确认。')}</p>}{detail.idempotency_parameter&&<p>{t('幂等键参数')}：<code>{detail.idempotency_parameter}</code></p>}</>}
      {selected.connector_id&&<footer><button className="primary" onClick={()=>{const id=selected.connector_id!;setSelected(null);onManage(id);}}><Plug size={15}/>{t('前往连接器配置')}</button></footer>}</div>}
    </dialog>
  </section>;
}
