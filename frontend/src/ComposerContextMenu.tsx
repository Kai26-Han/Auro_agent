import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowLeft, BookOpen, ChevronRight, LockKeyhole, Plug, Plus, Sparkles, Users, X } from 'lucide-react';
import { t } from './i18n';
import { PartnerPicker } from './PartnerPicker';
import { SkillPicker } from './SkillPicker';
import { ConnectorPicker } from './ConnectorPicker';
import { KnowledgePicker } from './KnowledgePicker';
import type { Partner, PartnerDefinition } from './PartnerCatalog';
import type { Skill, SkillRef } from './SkillCatalog';
import type { ConnectorSummary } from './ConnectorCatalog';
import type { KnowledgeBase } from './KnowledgeCenter';
import { connectorSelections, missingConnectorTools } from './connectorSelection';
import './composer-context.css';

type Section='partners'|'skills'|'connectors'|'knowledge';
type Props={
  partners:Partner[];partner:PartnerDefinition|null;onPartner:(p:PartnerDefinition)=>void;
  skills:Skill[];skillRefs:SkillRef[];onSkills:(refs:SkillRef[])=>void;boundSkill:boolean;
  connectors:ConnectorSummary[];connectorIds:string[];onConnectors:(ids:string[])=>void;onRefreshConnectors:()=>void;
  bases:KnowledgeBase[];kbIds:string[];onKnowledge:(ids:string[])=>void;
  locked:boolean;onManage:(section:Section)=>void;
};

export function ComposerContextMenu(props:Props) {
  const {partners,partner,onPartner,skills,skillRefs,onSkills,boundSkill,connectors,connectorIds,onConnectors,onRefreshConnectors,bases,kbIds,onKnowledge,locked,onManage}=props;
  const [open,setOpen]=useState(false);const [section,setSection]=useState<Section|null>(null);
  const root=useRef<HTMLDivElement>(null);const trigger=useRef<HTMLButtonElement>(null);const panel=useRef<HTMLDivElement>(null);
  const rowRefs=useRef<Partial<Record<Section,HTMLButtonElement|null>>>({});const previous=useRef<Section|null>(null);
  const close=useCallback(()=>{setOpen(false);setSection(null);trigger.current?.focus();},[]);
  const back=()=>{previous.current=section;setSection(null);};
  useEffect(()=>{if(locked){setOpen(false);setSection(null);}},[locked]);
  useEffect(()=>{
    if(!open)return;
    if(section){panel.current?.querySelector<HTMLElement>('input:not(:disabled), button:not(:disabled)')?.focus();}
    else {(previous.current?rowRefs.current[previous.current]:rowRefs.current.partners)?.focus();previous.current=null;}
  },[open,section]);
  useEffect(()=>{
    if(!open)return;
    const outside=(e:PointerEvent)=>{if(!root.current?.contains(e.target as Node)){setOpen(false);setSection(null);}};
    const key=(e:KeyboardEvent)=>{if(e.key==='Escape'){e.preventDefault();if(section){previous.current=section;setSection(null);}else close();}};
    document.addEventListener('pointerdown',outside);document.addEventListener('keydown',key);
    return()=>{document.removeEventListener('pointerdown',outside);document.removeEventListener('keydown',key);};
  },[open,section,close]);
  const skillHint=['fixed','dynamic'].includes(partner?.execution_mode||'')?t('协作伙伴按成员配置技能，请到技能广场编辑。'):t('此伙伴已固定技能版本；更换技能请编辑伙伴并新建对话。');
  const skill=skills.find(s=>s.id===skillRefs[0]?.id);
  const selectedConnectors=connectorSelections(connectors,connectorIds).filter(row=>row.selectedIds.length);
  const missingConnectorIds=missingConnectorTools(connectors,connectorIds);
  const connectorSummary=selectedConnectors.length===1?selectedConnectors[0].connector.name:selectedConnectors.length?t('已选 {0} 个连接器',selectedConnectors.length):missingConnectorIds.length?t('失效的选择'):t('未选择');
  const rows=[
    {id:'partners' as const,label:t('伙伴'),icon:Users,selected:!!partner,summary:partner?.name || t('工作台助手'),disabled:false},
    {id:'skills' as const,label:t('技能'),icon:Sparkles,selected:!!skillRefs.length,summary:skillRefs.length?skill?.display_name || skillRefs[0].name || t('不可用技能'):t('未选择'),disabled:boundSkill},
    {id:'connectors' as const,label:t('连接器'),icon:Plug,selected:!!connectorIds.length,summary:connectorSummary,disabled:false},
    {id:'knowledge' as const,label:t('知识库'),icon:BookOpen,selected:!!kbIds.length,summary:kbIds.length===1?bases.find(kb=>kb.id===kbIds[0])?.name || t('已删除或归档知识库'):kbIds.length?t('已选 {0} 个知识库',kbIds.length):t('未选择'),disabled:false},
  ];
  const selected=rows.filter(row=>row.selected);
  function show(next:Section) {if(locked)return;setSection(next);setOpen(true);}
  function manage(next:Section) {setOpen(false);setSection(null);onManage(next);}
  return <div className="composer-context-control" ref={root} onBlur={e=>{if(e.relatedTarget&&!e.currentTarget.contains(e.relatedTarget as Node)){setOpen(false);setSection(null);}}}>
    <button ref={trigger} type="button" className={'composer-add-button '+(open?'is-open':'')} aria-label={t('添加到对话')} title={t('选择伙伴、技能、连接器或知识库')} aria-haspopup="dialog" aria-expanded={open} disabled={locked} onClick={()=>{setSection(null);setOpen(!open);}}>{open?<X size={18}/>:<Plus size={20}/>}</button>
    {selected.length>0&&<div className="composer-context-tags" aria-label={t('已选对话配置')}>{selected.map(row=><button type="button" key={row.id} disabled={locked||row.disabled} className="composer-context-tag" title={row.disabled?skillHint:row.label+' · '+row.summary} aria-label={t('已选{0}：{1}',row.label,row.summary)} onClick={()=>show(row.id)}><row.icon size={13}/><span>{row.summary}</span>{row.disabled&&<LockKeyhole size={11}/>}</button>)}</div>}
    {open&&<div className={'composer-context-popup '+(section?'has-panel':'')} role="dialog" aria-label={t('对话配置')}>
      {section?<><div className="composer-context-panel-bar"><button type="button" onClick={back} aria-label={t('返回添加菜单')}><ArrowLeft size={15}/>{t('返回')}</button><button type="button" onClick={close}>{t('完成')}</button></div><div className="composer-context-panel" ref={panel}>
        {section==='partners'&&<PartnerPicker embedded onDismiss={close} partners={partners} value={partner} locked={locked} onChange={onPartner} onManage={()=>manage('partners')}/>}
        {section==='skills'&&<SkillPicker embedded onDismiss={close} skills={skills} value={skillRefs} locked={locked||boundSkill} onChange={onSkills} onManage={()=>manage('skills')}/>}
        {section==='connectors'&&<ConnectorPicker embedded onDismiss={close} connectors={connectors} value={connectorIds} allowed={partner?partner.connector_tool_ids || []:undefined} locked={locked} onChange={onConnectors} onRefresh={onRefreshConnectors} onManage={()=>manage('connectors')}/>}
        {section==='knowledge'&&<KnowledgePicker embedded onDismiss={close} bases={bases} value={kbIds} busy={locked} locked={locked} onChange={onKnowledge} onManage={()=>manage('knowledge')}/>}
      </div></>:<div className="composer-context-entries">{rows.map(row=><button ref={node=>{rowRefs.current[row.id]=node;}} type="button" key={row.id} className={row.selected?'is-selected':''} disabled={row.disabled} title={row.disabled?skillHint:undefined} onClick={()=>show(row.id)}><row.icon size={17}/><span className="context-entry-label">{row.label}</span><span className="context-entry-summary">{row.disabled?t(partner?.execution_mode==='dynamic'?'按成员配置':partner?.execution_mode==='fixed'?'按阶段配置':'伙伴已绑定'):row.summary}</span>{row.disabled?<LockKeyhole size={13}/>:<ChevronRight size={14}/>}</button>)}</div>}
    </div>}
  </div>;
}
