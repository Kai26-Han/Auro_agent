import { useEffect, useState } from 'react';
import { Search, X } from 'lucide-react';
import { api } from './api';
import { t } from './i18n';

type Snapshot = {content:string; category:string; version:number; source_quote:string; manual:boolean;status?:string;locked?:boolean;conditions?:string};
type Change = {id:number; action:string; before_value:Snapshot|null; after_value:Snapshot|null; source_thread:string|null; created:string};
type Run = {run_id:string; source_thread:string|null; engine:string; status:string; reason:string; summary:Record<string,number>; created:string};
type Page<T> = {items:T[];total:number;page:number};
const actions:Record<string,string> = {HOT_ADD:'对话中记住',HOT_UPDATE:'对话中修改',HOT_ARCHIVE:'对话中停用',ADD:'自动新增',UPDATE:'自动更新',MANUAL_ADD:'手动新增',MANUAL_UPDATE:'手动更新',DELETE:'删除',ARCHIVE:'归档',RESTORE:'恢复',REVIEW_ACCEPT:'采用建议'};
const reasons:Record<string,string> = {conflicts_pending:'有新的待处理建议',changes_applied:'已保存记忆变化',no_change:'已有记忆无需变化',no_facts:'没有值得长期保留的新信息',candidates_rejected:'候选未通过来源、版本或保护检查',learning_disabled:'本轮未开启记忆学习',config_changed:'配置已变化，本轮结果未写入',excluded_input:'输入包含不记录要求或敏感信息，已跳过',cancelled:'任务已取消',engine_failed:'引擎处理失败，请测试模型配置'};
const statuses:Record<string,string> = {completed:'整理完成',disabled:'未开启',skipped:'已跳过',failed:'处理失败'};

function Pages({page,total,onChange}:{page:number;total:number;onChange:(page:number)=>void}) {
  return total>30 ? <div className="button-row"><button className="secondary" disabled={page<=1} onClick={()=>onChange(page-1)}>{t('上一页')}</button><span>{page} / {Math.ceil(total/30)}</span><button className="secondary" disabled={page*30>=total} onClick={()=>onChange(page+1)}>{t('下一页')}</button></div> : null;
}

export function MemoryHistory({id,onClose,onSource}:{id:string;onClose:()=>void;onSource:(id:string)=>void}) {
  const [data,setData]=useState<Page<Change>|null>(null);
  const [page,setPage]=useState(1);
  const [error,setError]=useState('');
  useEffect(()=>{let active=true;setData(null);api<Page<Change>>(`/memory/items/${id}/history?page=${page}`).then(v=>{if(active)setData(v)}).catch(e=>{if(active)setError(e.message)});return()=>{active=false}},[id,page]);
  useEffect(()=>{const close=(e:KeyboardEvent)=>{if(e.key==='Escape')onClose()};window.addEventListener('keydown',close);return()=>window.removeEventListener('keydown',close)},[onClose]);
  return <div className="modal-backdrop"><section className="modal memory-history-modal" role="dialog" aria-modal="true" aria-labelledby="memory-history-title"><header><div><span className="eyebrow">MEMORY HISTORY</span><h2 id="memory-history-title">{t('变化记录')}</h2></div><button className="secondary" aria-label={t('关闭')} onClick={onClose}><X size={18}/></button></header><div className="modal-scroll"><p className="field-hint">{t('展示本次升级后保存的变化。旧版本未保存的历史正文无法恢复。')}</p>{error&&<p role="alert" className="knowledge-error">{t(error)}</p>}{!data&&!error?<p>{t('正在加载…')}</p>:data?.items.length ? data.items.map(c=><article className="memory-change" key={c.id}><div className="memory-card-top"><b>{t(actions[c.action]||c.action)}</b><time>{new Date(c.created).toLocaleString()}</time></div>{c.before_value&&<div className="memory-change-before"><small>{t('修改前')} · v{c.before_value.version}</small><p>{c.before_value.content}</p></div>}{c.after_value&&<div className="memory-change-after"><small>{t('修改后')} · v{c.after_value.version}</small><p>{c.after_value.content}</p>{c.after_value.status&&<small>{t(c.after_value.status==='active'?'生效中':'已归档')} · {t(c.after_value.locked?'已锁定':'自动维护')}</small>}{c.after_value.conditions&&<p>{t('适用条件')}：{c.after_value.conditions}</p>}{c.after_value.source_quote&&<details><summary>{t('原文依据')}</summary><blockquote>{c.after_value.source_quote}</blockquote></details>}</div>}{c.source_thread&&<button className="back-link" onClick={()=>onSource(c.source_thread!)}>{t('打开来源对话')}</button>}</article>):!error&&<p className="field-hint">{t('这条记忆尚无可查看的变化记录。')}</p>}{data&&<Pages page={page} total={data.total} onChange={setPage}/>}</div></section></div>;
}

export function MemoryRuns({space,refreshKey,onSource}:{space:string;refreshKey:boolean;onSource:(id:string)=>void}) {
  const [data,setData]=useState<Page<Run>|null>(null);
  const [page,setPage]=useState(1);
  const [error,setError]=useState('');
  useEffect(()=>{let active=true;setData(null);setError('');api<Page<Run>>(`/memory/spaces/${space}/runs?page=${page}`).then(v=>{if(active)setData(v)}).catch(e=>{if(active)setError(e.message)});return()=>{active=false}},[space,page,refreshKey]);
  return <section className="memory-insights"><h2>{t('每轮记忆如何整理')}</h2><p className="field-hint">{t('区分引擎没有提取新信息、保持原记忆、更新记忆和处理失败。这里只记录升级后的处理过程。')}</p>{error&&<p role="alert" className="knowledge-error">{t(error)}</p>}{!data&&!error?<p>{t('正在加载…')}</p>:data?.items.length ? <div className="memory-run-list">{data.items.map(r=><article className="memory-run" key={r.run_id}><div className="memory-card-top"><b>{r.engine==='mem0'?'Mem0 OSS':'LangMem'} · {t(statuses[r.status]||r.status)}</b><time>{new Date(r.created).toLocaleString()}</time></div><p>{t(reasons[r.reason]||r.reason)}</p>{r.status==='completed'&&<div className="memory-run-counts"><span>{t('新增 {0}',r.summary.added||0)}</span><span>{t('更新 {0}',r.summary.updated||0)}</span><span>{t('未变化 {0}',r.summary.unchanged||0)}</span><span>{t('未通过校验 {0}',r.summary.rejected||0)}</span>{!!r.summary.conflicts&&<span>{t('待处理 {0}',r.summary.conflicts)}</span>}</div>}{r.source_thread&&<button className="back-link" onClick={()=>onSource(r.source_thread!)}>{t('打开来源对话')}</button>}</article>)}</div>:!error&&<div className="memory-empty"><h2>{t('还没有整理记录')}</h2><p>{t('完成下一轮普通助手对话后，可以在这里查看记忆处理结果。')}</p></div>}{data&&<Pages page={page} total={data.total} onChange={setPage}/>}</section>;
}

type Recall = {enabled:boolean;engine:string;method:string;limit:number;context_chars:number;scope_limit:number;items:{id:string;content:string;score:number|null;included:boolean;version:number;memory_type?:string;reason?:string}[]};
export function MemoryRecall({space,onHistory,scopeKind='personal',scopeId='personal'}:{space:string;onHistory:(id:string)=>void;scopeKind?:string;scopeId?:string}) {
  const [query,setQuery]=useState('');
  const [busy,setBusy]=useState(false);
  const [result,setResult]=useState<Recall|null>(null);
  const [error,setError]=useState('');
  return <section className="memory-insights"><h2>{t('查看问题会召回哪些记忆')}</h2><p className="field-hint">{t('使用当前空间的引擎、相似度阈值和上下文预算测试。不会生成回答或新增记忆；语义检索会调用配置的嵌入模型。')}</p><form className="memory-recall-form" onSubmit={async e=>{e.preventDefault();if(busy||!query.trim())return;setBusy(true);setError('');setResult(null);try{setResult(await api<Recall>(`/memory/spaces/${space}/recall`,{method:'POST',body:JSON.stringify({query,scope_kind:scopeKind,scope_id:scopeId})}))}catch(e){setError((e as Error).message)}finally{setBusy(false)}}}><label>{t('测试问题')}<textarea required maxLength={1000} value={query} rows={3} onChange={e=>setQuery(e.target.value)} placeholder={t('例如：按我的学习习惯安排练习')}/></label><button className="primary" disabled={busy||!query.trim()}><Search size={15}/>{t(busy?'正在检索…':'测试召回')}</button></form>{error&&<p role="alert" className="knowledge-error">{t(error)}</p>}{result&&<><p className="field-hint">{result.engine==='mem0'?'Mem0 OSS':'LangMem'} · {t(result.method==='semantic'?'语义检索':'关键词与偏好检索')} · {t('最多 {0} 条，预算 {1} 字符',result.limit,result.context_chars)}</p>{!result.enabled?<p className="knowledge-notice">{t('全局记忆读取已关闭，请先在记忆引擎中开启。')}</p>:!result.items.length?<p className="knowledge-notice">{t('没有召回到符合条件的记忆。可检查空间、检索方式和相似度阈值。')}</p>:<div className="memory-grid">{result.items.map((r,i)=><article className="memory-card" key={r.id}><div className="memory-card-top"><span>#{i+1} · v{r.version}</span><small>{r.memory_type==='profile'?t('档案固定读取'):r.score===null?t('关键词排序'):t('相似度 {0}',r.score.toFixed(3))}</small></div><p className="memory-content">{r.content}</p><span className="memory-badge">{t(r.included?'会放入回答上下文':'超出上下文预算')}</span><button className="back-link" onClick={()=>onHistory(r.id)}>{t('变化记录')}</button></article>)}</div>}<p className="field-hint">{t('相似度是检索分数，不是事实可信度。检索范围覆盖全部有效事实，最终按相关性和上下文预算选择。')}</p></>}</section>;
}
