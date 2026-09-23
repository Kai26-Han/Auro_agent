import { useState } from 'react';
import { Sparkles } from 'lucide-react';
import { api } from './api';
import { t } from './i18n';
import type { Partner, PartnerDefinition } from './PartnerCatalog';
import './capability-suggestions.css';

type Suggestion={id:string;version:string;name:string;kind:string;matches:string[]};
export function CapabilitySuggestions({text,partners,onSelect}:{text:string;partners:Partner[];onSelect:(p:PartnerDefinition)=>void}) {
  const [items,setItems]=useState<Suggestion[]|null>(null),[query,setQuery]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const fresh=query===text;
  return <div className="capability-suggestions"><button type="button" className="skill-text-button" disabled={busy||text.trim().length<2} onClick={async()=>{setBusy(true);setError('');const current=text;try{setItems(await api<Suggestion[]>('/capability-studio/suggest',{method:'POST',body:JSON.stringify({text:current})}));setQuery(current);}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}><Sparkles size={13}/>{t('推荐适合的伙伴')}</button>
    {error&&<p role="alert">{error}</p>}{fresh&&items&&<><p>{t(items.length?'按名称与简介匹配，点击后才切换伙伴。':'没有明确匹配，继续使用当前助手即可。')}</p>{items.map(item=><button type="button" className="secondary" key={item.id} onClick={()=>{const p=partners.find(p=>p.id===item.id&&p.version===item.version&&p.enabled&&!p.archived);if(p)onSelect(p);else setError(t('配置已变化，请重新获取推荐。'));}}>{item.name} · {t(item.kind==='team'?'团队':item.kind==='workflow'?'流程':'助手')}</button>)}</>}
  </div>;
}
