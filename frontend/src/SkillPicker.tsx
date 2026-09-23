import { usePickerState, type PickerPresentation } from './usePickerState';
import { useEffect, useRef, useState } from 'react';
import { Check, ChevronDown, Search, Sparkles, X } from 'lucide-react';
import { t } from './i18n';
import { type Skill, type SkillRef, skillLabel } from './SkillCatalog';

export function SkillPicker({ skills, value, locked, onChange, onManage, embedded=false, onDismiss }: PickerPresentation & { skills: Skill[]; value: SkillRef[]; locked: boolean; onChange:(value:SkillRef[])=>void; onManage:()=>void }) {
  const [open,setOpen]=usePickerState(embedded,onDismiss); const [query,setQuery]=useState('');
  const root=useRef<HTMLDivElement>(null); const trigger=useRef<HTMLButtonElement>(null);
  const ref=value[0]; const selected=skills.find(s=>s.id===ref?.id);
  const title=value.length===1 ? selected?.display_name || ref.name || t('不可用技能') : value.length>1 ? t('已选择 {0} 个技能',value.length) : t('技能');
  useEffect(()=>{if(locked)setOpen(false);},[locked]);
  useEffect(()=>{if(!open||embedded)return; const outside=(e:PointerEvent)=>{if(!root.current?.contains(e.target as Node))setOpen(false);};const key=(e:KeyboardEvent)=>{if(e.key==='Escape'){setOpen(false);trigger.current?.focus();}}; document.addEventListener('pointerdown',outside);document.addEventListener('keydown',key);return()=>{document.removeEventListener('pointerdown',outside);document.removeEventListener('keydown',key);};},[open,embedded]);
  return <div className={'composer-knowledge composer-skill '+(embedded?'picker-embedded':'')} ref={root} onBlur={e=>{if(!embedded&&!e.currentTarget.contains(e.relatedTarget as Node))setOpen(false);}}>{!embedded&&<button ref={trigger} type="button" className={'knowledge-chip '+(ref?'has-selection':'')} aria-label={t('选择技能：')+title} aria-haspopup="dialog" aria-expanded={open} disabled={locked} onClick={()=>setOpen(!open)}><Sparkles size={16}/><span>{title}</span><ChevronDown size={13}/></button>}
    {open && <div className="knowledge-popover skill-picker-popover" role={embedded?'region':'dialog'} aria-label={t('选择技能')}><header><b>{t('选择技能')}</b><small>{t('最多组合 3 个 · 自动技能会补足剩余名额')}</small></header><label className="skill-picker-search"><Search size={15}/><input aria-label={t('搜索可用技能')} placeholder={t('搜索技能')} value={query} onChange={e=>setQuery(e.target.value)}/></label>
      {ref && selected && selected.revision!==ref.revision && <div className="skill-update-hint"><p>{t('此技能有新版本，当前对话仍使用原版本。')}</p><button type="button" className="skill-text-button" disabled={!selected.enabled || selected.archived || !selected.compatible || !selected.runtime_ready} onClick={()=>onChange([{id:selected.id,revision:selected.revision,name:selected.display_name,version:selected.version}])}>{t('改用最新版本')}</button></div>}
      {ref && (!selected || !selected.enabled || selected.archived || !selected.runtime_ready || !selected.revisions.find(r=>r.revision===ref.revision)?.compatible) && <p className="knowledge-scope-hint">{t('所选技能不可用，请清除选择或在技能广场处理。')}</p>}
      <div className="knowledge-options skill-picker-options">{skills.filter(s=>!s.archived&&!s.internal_only&&s.user_invocable&&(s.display_name+s.description).toLocaleLowerCase().includes(query.toLocaleLowerCase())).map(s=>{const current=value.find(v=>v.id===s.id);const available=s.enabled&&s.compatible&&!!s.runtime_ready;return <button className="skill-picker-option" type="button" key={s.id} role="checkbox" aria-checked={!!current} disabled={!available||(!current&&value.length>=3)} onClick={()=>onChange(current?value.filter(v=>v.id!==s.id):[...value,{id:s.id,revision:s.revision,name:s.display_name,version:s.version}])}>
        <span className="skill-picker-option-icon" aria-hidden="true"><Sparkles size={14}/></span><span className="skill-picker-option-copy"><span className="skill-picker-option-title"><b>{s.display_name}</b>{!available&&<em>{skillLabel(s)}</em>}</span><small title={s.description}>{s.description}</small></span><span className="skill-picker-check" aria-hidden="true">{current&&<Check size={13}/>}</span>
      </button>;})}</div>
      {!skills.some(s=>!s.archived&&s.user_invocable&&!s.internal_only) && <p className="knowledge-scope-hint">{t('先到技能广场导入或添加可手动使用的技能。')}</p>}<footer className="skill-picker-footer"><span>{t('已选择 {0}/3 个技能',value.length)}</span><div><button type="button" disabled={!ref} onClick={()=>onChange([])}><X size={14}/>{t('清空选择')}</button><button type="button" onClick={()=>{setOpen(false);onManage();}}>{t('管理技能')}</button></div></footer></div>}
  </div>;
}
