import { useEffect, useState } from 'react';
import { ArrowLeft, GitBranch, Plus, Play, Save } from 'lucide-react';
import { api } from './api';
import { t } from './i18n';
import { ExpertFields, type Expert } from './WorkflowCatalog';
import type { Partner, PartnerDefinition } from './PartnerCatalog';
import type { Skill } from './SkillCatalog';
import type { ConnectorSummary } from './ConnectorCatalog';

type TeamInput = {name:string;description:string;experts:Record<string,Expert>;planning:string;synthesis:string;max_tasks:number;concurrency:number;max_retries:number;task_timeout:number;max_model_calls:number;max_tokens:number};
type Team = TeamInput & {id:string;version:string;version_number:number;enabled:boolean;archived:boolean};
const inputOf=(v:TeamInput):TeamInput=>({name:v.name,description:v.description,experts:v.experts,planning:v.planning,synthesis:v.synthesis,max_tasks:v.max_tasks,concurrency:v.concurrency,max_retries:v.max_retries,task_timeout:v.task_timeout,max_model_calls:v.max_model_calls,max_tokens:v.max_tokens});
export function TeamCatalog({partners,skills,connectors,tools,locked,onChanged,onUse}:{partners:Partner[];skills:Skill[];connectors:ConnectorSummary[];tools:{id:string;name:string}[];locked:boolean;onChanged:()=>Promise<void>;onUse:(p:PartnerDefinition)=>void}) {
  const [teams,setTeams]=useState<Team[]>([]),[templates,setTemplates]=useState<TeamInput[]>([]);
  const [draft,setDraft]=useState<TeamInput|null>(null),[editing,setEditing]=useState<string|null>(null);
  const [error,setError]=useState(''),[saving,setSaving]=useState(false),[archived,setArchived]=useState(false),[advanced,setAdvanced]=useState(false),[json,setJson]=useState('');
  const load=async()=>{const [a,b]=await Promise.all([api<Team[]>('/teams'),api<TeamInput[]>('/teams/templates')]);setTeams(a);setTemplates(b);};
  useEffect(()=>{load().catch(e=>setError(e.message));},[]);
  function edit(v:TeamInput,id:string|null=null){setDraft(structuredClone(inputOf(v)));setEditing(id);setAdvanced(false);setError('');}
  async function act(fn:()=>Promise<unknown>){setSaving(true);setError('');try{await fn();await load();await onChanged();}catch(e){setError((e as Error).message);}finally{setSaving(false);}}
  const expert=(key:string,patch:Partial<Expert>)=>setDraft(d=>d?{...d,experts:{...d.experts,[key]:{...d.experts[key],...patch}}}:d);
  return <section className="workflow-catalog">
    {error&&<div className="skill-error" role="alert">{error}</div>}
    {draft?<div className="workflow-editor"><button className="back-link" disabled={saving} onClick={()=>setDraft(null)}><ArrowLeft size={15}/>{t('返回团队列表')}</button><h2>{t(editing?'编辑团队':'创建团队')}</h2><p className="skill-detail-hint">{t('团长先制定任务计划，成员按依赖并行执行，团长核对分歧并汇总成果。')}</p>
      <div className="workflow-edit-mode"><button aria-pressed={!advanced} disabled={saving} onClick={()=>{if(advanced)act(async()=>{setDraft(await api<TeamInput>('/teams/validate',{method:'POST',body:json}));setAdvanced(false);});}}>{t('常用配置')}</button><button aria-pressed={advanced} disabled={saving} onClick={()=>{if(!advanced){setJson(JSON.stringify(draft,null,2));setAdvanced(true);}}}>{t('高级 JSON 配置')}</button></div>
      {advanced?<><p className="skill-detail-hint">{t('可增删成员；leader 为团长。任务计划由模型生成并校验，不执行生成的代码。')}</p><textarea className="workflow-json" aria-label={t('团队 JSON 定义')} value={json} onChange={e=>setJson(e.target.value)}/></>:<>
        <label className="skill-field">{t('团队名称')}<input maxLength={80} value={draft.name} onChange={e=>setDraft({...draft,name:e.target.value})}/></label>
        <label className="skill-field">{t('简介')}<input maxLength={1000} value={draft.description} onChange={e=>setDraft({...draft,description:e.target.value})}/></label>
        <div className="team-process"><span>{t('团长规划')}</span><span>→</span><span>{t('依赖调度与并行执行')}</span><span>→</span><span>{t('汇总交付')}</span></div>
        <label className="skill-field">{t('任务拆解策略')}<textarea rows={4} maxLength={4000} value={draft.planning} onChange={e=>setDraft({...draft,planning:e.target.value})}/></label>
        <label className="skill-field">{t('冲突仲裁与输出要求')}<textarea rows={4} maxLength={4000} value={draft.synthesis} onChange={e=>setDraft({...draft,synthesis:e.target.value})}/></label>
        <h3>{t('团长与成员')}</h3><p className="skill-detail-hint">{t('每项任务有独立上下文；角色可复用，成员只能读取指定前置成果。')}</p>
        <button type="button" className="secondary" disabled={Object.keys(draft.experts).length>=8} onClick={()=>{let i=1;while(draft.experts['member_'+i])i++;setDraft({...draft,experts:{...draft.experts,['member_'+i]:{name:t('新成员'),instructions:t('完成分配的任务，说明依据和限制。'),tool_ids:[],connector_tool_ids:[],skill_refs:[]}}});}}><Plus size={14}/>{t('添加成员')}</button><ExpertFields onRemove={key=>{const experts={...draft.experts};delete experts[key];setDraft({...draft,experts});}} experts={draft.experts} expert={expert} partners={partners} skills={skills} tools={tools} connectors={connectors}/>
        <div className="partner-form-grid">
          <label className="skill-field">{t('最多子任务')}<input type="number" min={1} max={6} value={draft.max_tasks} onChange={e=>setDraft({...draft,max_tasks:Number(e.target.value)})}/></label>
          <label className="skill-field">{t('最大并发')}<select value={draft.concurrency} onChange={e=>setDraft({...draft,concurrency:Number(e.target.value)})}><option value={1}>1</option><option value={2}>2</option></select></label>
          <label className="skill-field">{t('失败重试次数')}<select value={draft.max_retries} onChange={e=>setDraft({...draft,max_retries:Number(e.target.value)})}><option value={0}>0</option><option value={1}>1</option></select></label>
          <label className="skill-field">{t('单任务超时（秒）')}<input type="number" min={10} max={600} value={draft.task_timeout} onChange={e=>setDraft({...draft,task_timeout:Number(e.target.value)})}/></label>
          <label className="skill-field">{t('总模型调用上限')}<input type="number" min={4} max={80} value={draft.max_model_calls} onChange={e=>setDraft({...draft,max_model_calls:Number(e.target.value)})}/></label>
          <label className="skill-field">{t('总 Token 预算')}<input type="number" min={10000} max={500000} value={draft.max_tokens} onChange={e=>setDraft({...draft,max_tokens:Number(e.target.value)})}/></label>
        </div><p className="skill-detail-hint">{t('团长和成员共享总预算。恢复沿用已保存计划和成果，不重新规划。')}</p>
      </>}
      <div className="button-row"><button className="primary" disabled={saving} onClick={()=>act(async()=>{await api(editing?'/teams/'+editing:'/teams',{method:editing?'PUT':'POST',body:advanced?json:JSON.stringify(draft)});setDraft(null);})}><Save size={15}/>{t('保存团队')}</button><button className="secondary" disabled={saving} onClick={()=>setDraft(null)}>{t('取消')}</button></div>
    </div>:<><div className="skill-catalog-toolbar"><div className="skill-section-label"><h2>{t('团队')}</h2><p>{t('按目标动态拆解任务，让合适的成员协作完成。')}</p></div><button className="skill-text-button" onClick={()=>setArchived(!archived)}>{t(archived?'查看可用团队':'查看归档团队')}</button></div>
      <div className="workflow-grid">{teams.filter(v=>v.archived===archived).map(v=><article className="workflow-card" key={v.id}><header><GitBranch size={20}/><h3>{v.name}</h3><small>v{v.version_number}</small></header><p>{v.description}</p><div className="workflow-mini-stages">{Object.entries(v.experts).map(([key,e])=><span key={key}>{key==='leader'?t('团长')+' · ':''}{e.name}</span>)}</div><small>{t('最多 {0} 个子任务 · {1} 个并发',v.max_tasks,v.concurrency)}</small><footer><button className="primary" disabled={locked||saving||!v.enabled||v.archived} onClick={()=>{const p=partners.find(p=>p.id===v.id);if(p)onUse(p);}}><Play size={13}/>{t('用于对话')}</button><button className="skill-text-button" onClick={()=>edit(v,v.id)}>{t('编辑')}</button><button className="skill-text-button" disabled={saving} onClick={()=>act(()=>api('/teams/'+v.id,{method:'PATCH',body:JSON.stringify(v.archived?{archived:false}:{enabled:!v.enabled})}))}>{t(v.archived?'恢复':v.enabled?'停用':'启用')}</button>{!v.archived&&<button className="skill-text-button" disabled={saving} onClick={()=>act(()=>api('/teams/'+v.id,{method:'PATCH',body:JSON.stringify({archived:true})}))}>{t('归档')}</button>}</footer></article>)}</div>
      {!archived&&<><h3 className="workflow-template-title">{t('从示例开始')}</h3><div className="workflow-templates">{templates.map(v=><button key={v.name} onClick={()=>edit(v)}><Plus size={18}/><span><b>{t(v.name)}</b><small>{t(v.description)}</small></span></button>)}</div></>}
    </>}
  </section>;
}
