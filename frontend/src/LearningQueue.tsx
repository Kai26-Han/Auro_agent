import { useEffect, useState } from 'react';
import { Clock3, RefreshCw } from 'lucide-react';
import { api } from './api';
import { t } from './i18n';

type LearningJob = {id:string;seq:number;thread_id:string;run_id:string;scope_kind:string;scope_id:string;state:string;display_state:string;reason:string;attempts:number;available:number;created:number;summary:Record<string,number>};
type Queue = {items:LearningJob[];total:number;counts:Record<string,number>;paused:boolean;cursors:{thread_id:string;seq:number}[]};
const states:Record<string,string> = {awaiting_answer:'等待回答完成',pending:'等待整理',running:'正在整理',retry_wait:'等待自动重试',failed:'需要处理',completed:'整理完成',skipped:'已跳过',cancelled:'已取消',paused:'已暂停'};
const reasons:Record<string,string> = {engine_failed:'提取模型调用失败；最多自动尝试 3 次。请检查引擎模型设置后重试。',manual_state_changed:'空间内已有人工改动，旧任务已跳过，避免覆盖你的修改。',config_changed:'记忆设置已变化，旧任务已跳过。',excluded_input:'包含敏感信息或不记录要求，未进入整理。',user_cancelled:'你已取消此任务。',answer_not_completed:'回答未完成或被新提问替代，未整理此来源。',already_processed_or_invalid:'来源已处理或提交条件已变化。',profile_paused:'方案切换或服务关闭，等待恢复。',worker_recovered:'服务重启后已恢复任务。',lease_recovered:'已重新领取中断的任务。'};
const pendingCount=(data:Queue)=>['awaiting_answer','pending','running','retry_wait'].reduce((sum,k)=>sum+(data.counts[k]||0),0);
export function useLearningQueue(space:string,page=1){
  const [data,setData]=useState<Queue|null>(null);const [error,setError]=useState('');const [revision,setRevision]=useState(0);
  useEffect(()=>{let live=true;let timer:ReturnType<typeof setTimeout>;
    async function load(){try{const result=await api<Queue>(`/memory/spaces/${space}/learning?page=${page}`);if(live){setData(result);setError('')}}catch(e){if(live)setError((e as Error).message)}finally{if(live)timer=setTimeout(load,3000)}}
    setData(null);load();return()=>{live=false;clearTimeout(timer)};
  },[space,page,revision]);
  return {data,error,refresh:()=>setRevision(v=>v+1)};
}
export function LearningQueueStatus({space,onOpen}:{space:string;onOpen:()=>void}){
  const {data}=useLearningQueue(space);
  if(!data||(!pendingCount(data)&&!data.counts.failed))return null;
  return <button type="button" className="learning-status" onClick={onOpen}><Clock3 size={14}/><span>{t(data.paused?'此空间的后台整理已暂停':'此空间的记忆在后台整理')} · {t('待处理 {0} 条',pendingCount(data))}{!!data.counts.failed&&' · '+t('失败 {0} 条',data.counts.failed)}</span><span>{t('查看记忆中心')}</span></button>;
}
export function LearningQueuePanel({space,readOnly,onSource}:{space:string;readOnly:boolean;onSource:(id:string)=>void}){
  const [page,setPage]=useState(1);const {data,error,refresh}=useLearningQueue(space,page);const [busy,setBusy]=useState(false);const [actionError,setActionError]=useState('');
  async function act(path:string){setBusy(true);setActionError('');try{await api(`/memory/spaces/${space}/learning/${path}`,{method:'POST'});refresh()}catch(e){setActionError((e as Error).message)}finally{setBusy(false)}}
  return <section className="learning-queue" aria-label={t('整理队列')}><div className="learning-toolbar"><div><h3>{t('后台整理')}</h3><p className="field-hint">{t('仅处理已授权的新提问，按每个对话的来源顺序整理。失败任务会阻挡该对话后续来源；可重试或取消。')}</p></div><div className="button-row"><button className="secondary" disabled={busy} onClick={refresh} aria-label={t('刷新队列')}><RefreshCw size={15}/></button><button className="primary" disabled={busy||readOnly||data?.paused||!((data?.counts.pending||0)+(data?.counts.retry_wait||0))} onClick={()=>act('flush')}>{t('立即整理')}</button></div></div>
    {(error||actionError)&&<p role="alert" className="knowledge-error">{error||actionError}</p>}
    {data?.paused&&<p className="knowledge-notice">{t('当前方案未激活或后台学习已关闭，队列暂停。')}</p>}
    {data&&<div className="learning-counts"><span>{t('待处理 {0} 条',pendingCount(data))}</span><span>{t('失败 {0} 条',data.counts.failed||0)}</span><span>{t('已完成 {0} 条',data.counts.completed||0)}</span></div>}
    {!data?<p role="status">{t('正在加载…')}</p>:!data.items.length?<div className="memory-empty"><h3>{t('还没有后台整理任务')}</h3><p>{t('开启后台学习后，新提问会在这里留下处理进度；不会自动补录旧聊天。')}</p></div>:data.items.map(job=><article className="memory-change learning-job" key={job.id}><div className="memory-card-top"><b>#{job.seq} · {t(states[job.display_state]||job.display_state)}</b><time>{new Date(job.created*1000).toLocaleString()}</time></div><div className="memory-card-meta"><span>{t('尝试 {0} 次',job.attempts)}</span><span>{t(job.scope_kind==='personal'?'个人通用':job.scope_kind==='project'?'项目':'伙伴')}</span><span>{t('已处理至来源 #{0}',data.cursors.find(c=>c.thread_id===job.thread_id)?.seq||0)}</span></div>{job.reason&&<p className="field-hint">{t(reasons[job.reason]||job.reason)}</p>}{['pending','retry_wait'].includes(job.state)&&!data.paused&&<p className="field-hint">{t('最早开始时间')}：{new Date(job.available*1000).toLocaleTimeString()}</p>}{job.state==='completed'&&<p>{t('新增 {0} · 更新 {1} · 待确认 {2}',job.summary.added||0,job.summary.updated||0,job.summary.conflicts||0)}</p>}<div className="button-row"><button className="back-link" onClick={()=>onSource(job.thread_id)}>{t('打开来源对话')}</button>{['failed','retry_wait'].includes(job.state)&&<button className="secondary" disabled={busy||readOnly||data.paused} onClick={()=>act(`${job.id}/retry`)}>{t('重试整理')}</button>}{!['completed','skipped','cancelled'].includes(job.state)&&<button className="secondary" disabled={busy||readOnly} onClick={()=>act(`${job.id}/cancel`)}>{t('取消整理')}</button>}</div></article>)}
    {!!data&&data.total>30&&<div className="button-row"><button className="secondary" disabled={page<=1} onClick={()=>setPage(page-1)}>{t('上一页')}</button><span>{page} / {Math.ceil(data.total/30)}</span><button className="secondary" disabled={page*30>=data.total} onClick={()=>setPage(page+1)}>{t('下一页')}</button></div>}
  </section>;
}
