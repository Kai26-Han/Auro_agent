import { useEffect, useState } from 'react';
import { ArrowLeft, LoaderCircle, Plug, Sparkles, Users } from 'lucide-react';
import { ConnectorCatalog, type Connector, type ConnectorSummary } from './ConnectorCatalog';
import { api } from './api';
import { t, useLanguage } from './i18n';
import './skills.css';
import { SkillCatalog, type Skill, type SkillRef } from './SkillCatalog';
import { PartnerCatalog, type Partner, type PartnerDefinition } from './PartnerCatalog';
import { TeamCatalog } from './TeamCatalog';
import { WorkflowCatalog } from './WorkflowCatalog';

export type CenterTab = 'partners' | 'skills' | 'connectors';
type Catalog = { partners:Partner[];tools:{id:string;name:string;description:string}[];skills:Skill[];connectors:ConnectorSummary[] };
const tabs = [
  {id:'partners',label:'伙伴',icon:Users}, {id:'skills',label:'技能',icon:Sparkles},
  {id:'connectors',label:'连接器',icon:Plug},
] as const;

function Connections({onChanged}:{onChanged:()=>Promise<void>}) {
  const [items,setItems]=useState<Connector[]|null>(null);const [error,setError]=useState('');
  const load=async()=>{setError('');setItems(await api<Connector[]>('/connectors'));};
  useEffect(()=>{let disposed=false;api<Connector[]>('/connectors').then(items=>{if(!disposed)setItems(items);}).catch(e=>{if(!disposed)setError(e.message);});return()=>{disposed=true;};},[]);
  if(error)return <div className="skill-error" role="alert"><p>{error}</p><button className="secondary" onClick={()=>load().catch(e=>setError(e.message))}>{t('重新加载')}</button></div>;
  if(!items)return <div className="skill-loading"><LoaderCircle size={20} className="spin"/>{t('正在加载…')}</div>;
  return <ConnectorCatalog connectors={items} onChanged={async()=>{await load();await onChanged();}}/>;
}

export function SkillCenter({tab,onTab,onClose,onSkillsChanged,onUse,onUsePartner,locked}:{tab:CenterTab;onTab:(tab:CenterTab)=>void;onClose:()=>void;onSkillsChanged:()=>Promise<void>;onUse:(ref:SkillRef)=>void;onUsePartner:(p:PartnerDefinition)=>void;locked:boolean}) {
  useLanguage();
  const [catalog,setCatalog]=useState<Catalog|null>(null);const [error,setError]=useState('');
  const [partnerMode,setPartnerMode]=useState<'single'|'fixed'|'dynamic'>('single');
  const load=async()=>{setError('');setCatalog(await api<Catalog>('/skill-center?compact=true'));};
  useEffect(()=>{let disposed=false;api<Catalog>('/skill-center?compact=true').then(c=>{if(!disposed)setCatalog(c);}).catch(e=>{if(!disposed)setError(e.message);});return()=>{disposed=true;};},[]);
  const changed=async()=>{await load();await onSkillsChanged();};
  return <main className="skill-center"><div className="skill-content">
    <button className="back-link" onClick={onClose}><ArrowLeft size={15}/>{t('返回对话')}</button>
    <header className="skill-heading"><div><span className="eyebrow">{t('你的能力空间')}</span><h1>{t('技能广场')}</h1><p>{t('管理你的伙伴、技能与连接')}</p></div><span className="skill-heading-art" aria-hidden="true"><Sparkles size={28} strokeWidth={1.4}/></span></header>
    <nav className="skill-tabs" aria-label={t('技能广场分类')}>{tabs.map(item=><button key={item.id} aria-current={tab===item.id?'page':undefined} onClick={()=>onTab(item.id)}><item.icon size={17}/>{t(item.label)}{catalog&&<span>{item.id==='partners'?catalog.partners.length:item.id==='skills'?catalog.skills.filter(skill=>!skill.archived).length:catalog.connectors.length}</span>}</button>)}</nav>
    {error?<div className="skill-error" role="alert"><p>{error}</p><button className="secondary" onClick={()=>load().catch(e=>setError(e.message))}>{t('重新加载')}</button></div>:!catalog?<div className="skill-loading" role="status"><LoaderCircle className="spin" size={20}/>{t('正在加载…')}</div>:<>
      {tab==='partners'&&<><div className="partner-modes"><button aria-pressed={partnerMode==='single'} onClick={()=>setPartnerMode('single')}>{t('助手')}</button><button aria-pressed={partnerMode==='fixed'} onClick={()=>setPartnerMode('fixed')}>{t('流程')}</button><button aria-pressed={partnerMode==='dynamic'} onClick={()=>setPartnerMode('dynamic')}>{t('团队')}</button></div>{partnerMode==='single'?<PartnerCatalog connectors={catalog.connectors} partners={catalog.partners.filter(p=>!['fixed','dynamic'].includes(p.execution_mode||''))} skills={catalog.skills} tools={catalog.tools.filter(t=>!['skills_list','skill_view','read_skill_resource'].includes(t.id))} locked={locked} onUse={onUsePartner} onChanged={changed}/>:partnerMode==='dynamic'?<TeamCatalog key={catalog.partners.map(p=>p.version).join('|')} partners={catalog.partners} skills={catalog.skills} connectors={catalog.connectors} tools={catalog.tools.filter(t=>!['skills_list','skill_view','read_skill_resource'].includes(t.id))} locked={locked} onUse={onUsePartner} onChanged={changed}/>:<WorkflowCatalog key={catalog.partners.map(p=>p.version).join('|')} partners={catalog.partners} skills={catalog.skills} connectors={catalog.connectors} tools={catalog.tools.filter(t=>!['skills_list','skill_view','read_skill_resource'].includes(t.id))} locked={locked} onUse={onUsePartner} onChanged={changed}/>}</>}
      {tab==='skills'&&<SkillCatalog skills={catalog.skills} tools={catalog.tools.filter(t=>!['skills_list','skill_view','read_skill_resource'].includes(t.id))} locked={locked} onUse={onUse} onChanged={changed}/>}
      {tab==='connectors'&&<Connections onChanged={changed}/>}
    </>}
  </div></main>;
}
