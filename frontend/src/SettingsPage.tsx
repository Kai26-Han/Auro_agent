import { useState } from 'react';
import { Archive, ArrowLeft, Check, Cpu, FlaskConical, Globe2, Languages, Plus, Trash2 } from 'lucide-react';
import { api } from './api';
import { setLanguage, t, type Language } from './i18n';
import './settings.css';
import { ProviderLogo } from './ProviderLogo';
import { WebToolsSettings, type WebToolsConfig } from './WebToolsSettings';
import { DecisionToolsSettings, type DecisionToolsConfig } from './DecisionToolsSettings';
import { ArchivedSettings } from './ArchivedSettings';

type PricingInfo={status:'official'|'local'|'unavailable';currency?:'CNY'|'USD';rates?:Record<string,number>;source_url?:string;verified_at?:string;rate_tier?:string;reason?:string};
export type Profile = { id: string; name: string; provider: string; kind: 'chat' | 'embedding'; base_url: string; model: string; api_key?: string; api_key_set?: boolean; clear_key?: boolean; timeout: number; max_tokens: number; model_context_window?:number; context_window?: number; context_compaction_trigger?:number; context_compaction_target?:number; input_modalities?:('text'|'image')[]; pricing?:PricingInfo|null };
export type Preferences = { web_tools?:WebToolsConfig; decision_tools?:DecisionToolsConfig; language: Language; profiles: Profile[]; default_chat_id: string; default_embedding_id: string; providers: { id: string; name: string; base_url: string; kinds: string[] }[] };
type Section='general'|'models'|'network'|'experimental'|'data';

export function profileInput(profile: Profile) { const { api_key_set: _saved, pricing: _pricing, ...input } = profile; return input; }
function providerContextDefaults(provider:string) {
  if (provider === 'deepseek' || provider === 'gemini') return { model_context_window:1_000_000, context_window:256_000 };
  if (provider === 'anthropic') return { model_context_window:200_000, context_window:128_000 };
  if (provider === 'openai') return { model_context_window:128_000, context_window:128_000 };
  if (provider === 'ollama') return { model_context_window:32_768, context_window:32_768 };
  return { model_context_window:128_000, context_window:128_000 };
}
function knownModelContext(provider:string, model:string) {
  const key=`${provider}:${model.trim()}`;
  const known:Record<string,number>={
    'deepseek:deepseek-flash':1_000_000,'deepseek:deepseek-v4-flash':1_000_000,'deepseek:deepseek-v4-pro':1_000_000,
    'anthropic:claude-sonnet-5':1_000_000,'anthropic:claude-sonnet-4-6':1_000_000,'anthropic:claude-haiku-4-5-20251001':200_000,
    'openai:gpt-5.6-sol':1_050_000,'openai:gpt-5.6-terra':1_050_000,'openai:gpt-5.6-luna':1_050_000,
  };
  return known[key];
}

export function SettingsPage({ initial, onChanged, onClose, onArchiveChanged }: { initial: Preferences; onChanged: (v: Preferences) => void; onClose: () => void; onArchiveChanged:()=>Promise<void> }) {
  const [data, setData] = useState(initial);
  const [section,setSection]=useState<Section>('general');
  const [kind, setKind] = useState<'chat' | 'embedding'>('chat');
  const [id, setId] = useState(initial.default_chat_id);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [deleting, setDeleting] = useState<Profile | null>(null);
  const current = data.profiles.find(p => p.id === id);
  const profiles = data.profiles.filter(p => p.kind === kind);
  const chatProfiles=data.profiles.filter(p=>p.kind==='chat');
  const embeddingProfiles=data.profiles.filter(p=>p.kind==='embedding');
  const defaultChat=data.profiles.find(p=>p.id===data.default_chat_id);

  function edit(patch: Partial<Profile>) { setData(v => ({ ...v, profiles: v.profiles.map(p => p.id === id ? { ...p, ...patch } : p) })); }
  async function run(action: () => Promise<void>) { setBusy(true); setError(''); setNotice(''); try { await action(); } catch(e) { setError((e as Error).message); } finally { setBusy(false); } }
  function chooseKind(value:'chat'|'embedding') { setKind(value); setId(data[value === 'chat' ? 'default_chat_id' : 'default_embedding_id']); setNotice(''); setError(''); }
  function add() {
    const provider = kind === 'chat' ? 'deepseek' : 'ollama';
    const preset = data.providers.find(p => p.id === provider)!;
    const context = providerContextDefaults(provider);
    const profile: Profile = { id: crypto.randomUUID(), name: kind === 'chat' ? t('新对话模型') : t('新嵌入模型'), kind, provider, base_url: preset.base_url, model: '', api_key: '', timeout: 60, max_tokens: kind==='chat'?8192:2048, ...context, context_compaction_trigger:75, context_compaction_target:50, input_modalities:['text'] };
    setData({ ...data, profiles: [...data.profiles, profile] }); setId(profile.id);
  }
  async function save() {
    const result = await api<Preferences>('/settings/models', { method: 'PUT', body: JSON.stringify({ profiles: data.profiles.map(profileInput), default_chat_id: data.default_chat_id, default_embedding_id: data.default_embedding_id }) });
    setData(result); onChanged(result); setNotice(t('模型设置已保存，下次任务生效。'));
  }
  async function probe() {
    if (!current) return;
    const result = await api<{ dimension?: number; tool_calling?: boolean }>('/settings/probe', { method: 'POST', body: JSON.stringify(profileInput(current)) });
    setNotice(result.dimension ? t('连接成功 · 向量维度 {0}', result.dimension) : result.tool_calling ? t('连接成功，支持工具调用。') : t('连接成功，但未返回测试工具调用。请确认该模型支持工具调用后再用于资料问答。'));
  }
  const sections:{id:Section;label:string;icon:typeof Languages}[]=[
    {id:'general',label:t('常规'),icon:Languages},
    {id:'models',label:t('模型'),icon:Cpu},
    {id:'network',label:t('联网工具'),icon:Globe2},
    {id:'experimental',label:t('实验功能'),icon:FlaskConical},
    {id:'data',label:t('数据与归档'),icon:Archive},
  ];

  return <main className="knowledge-center settings-page">
    <header className="knowledge-header settings-header"><div><button className="back-link" onClick={onClose}><ArrowLeft size={16}/> {t('回到对话')}</button><span className="eyebrow">MAKE IT YOURS</span><h1>{t('设置')}</h1><p>{t('管理界面、模型与工具，让工作台按你的方式运行。')}</p></div><span className="settings-local-status"><i/>{t('配置保存在本机')}</span></header>
    <div className="settings-shell">
      <nav className="settings-side-nav" aria-label={t('设置分类')}><small>{t('设置分类')}</small>{sections.map(item=>{const Icon=item.icon;return <button key={item.id} aria-current={section===item.id?'page':undefined} onClick={()=>{setSection(item.id);setError('');setNotice('')}}><Icon size={16}/><span>{item.label}</span></button>})}</nav>
      <div className="settings-content">
        {section==='general'&&<section className="settings-view"><div className="settings-view-heading"><div><h2>{t('常规设置')}</h2><p>{t('管理界面语言与工作台的基础行为。')}</p></div></div><div className="settings-list">
          <article className="settings-row"><div><h3>{t('页面语言')}</h3><p>{t('切换界面语言，不翻译你的文档、笔记和对话内容。')}</p></div><select aria-label={t('页面语言')} disabled={busy} value={data.language} onChange={e=>{const language=e.target.value as Language;run(async()=>{await api('/settings/language',{method:'PUT',body:JSON.stringify({language})});const updated={...data,language};setLanguage(language);setData(updated);onChanged(updated)})}}><option value="zh">{t('简体中文')}</option><option value="en">English</option></select></article>
          <article className="settings-row"><div><h3>{t('新对话默认模型')}</h3><p>{defaultChat?`${defaultChat.name} · ${defaultChat.model}`:t('尚未配置')}</p></div><button className="secondary" onClick={()=>setSection('models')}>{t('管理模型配置')} →</button></article>
          <article className="settings-row"><div><h3>{t('本机数据')}</h3><p>{t('对话、配置、知识库与记忆均保存在当前设备。')}</p></div><span className="settings-badge">{t('本机保存')}</span></article>
        </div></section>}

        {section==='models'&&<section className="settings-view"><div className="settings-view-heading"><div><h2>{t('模型配置')}</h2><p>{t('集中管理语言模型和嵌入模型；对话时可以选择任一可用语言模型。')}</p></div><button className="primary" disabled={busy||data.profiles.length>=30} onClick={add}><Plus size={15}/>{t('添加模型')}</button></div>
          <div className="settings-model-summary"><article><small>{t('默认语言模型')}</small><b>{defaultChat?.name||t('尚未配置')}</b><span>{defaultChat?.model||'—'}</span></article><article><small>{t('语言模型')}</small><b>{chatProfiles.length}</b><span>{t('个配置')}</span></article><article><small>{t('RAG 嵌入模型')}</small><b>{embeddingProfiles.length}</b><span>{t('个配置')}</span></article></div>
          <div className="settings-model-toolbar"><div className="settings-kind-tabs" role="tablist" aria-label={t('模型类型')}><button role="tab" aria-selected={kind==='chat'} onClick={()=>chooseKind('chat')}>{t('语言模型')}</button><button role="tab" aria-selected={kind==='embedding'} onClick={()=>chooseKind('embedding')}>{t('RAG 嵌入模型')}</button></div><p>{kind==='chat'?t('模型需要支持工具调用；默认模型也用于 PageIndex 建索引。'):t('默认嵌入模型供新建 LlamaIndex 知识库使用；更换后已有知识库需要重建索引。')}</p></div>
          {error&&<div className="knowledge-error" role="alert">{error}</div>}{notice&&<div className="knowledge-notice" role="status">{notice}</div>}
          <div className="settings-model-layout"><aside className="settings-profile-list">{profiles.map(p=><button key={p.id} aria-pressed={id===p.id} onClick={()=>{setId(p.id);setNotice('')}}><ProviderLogo provider={p.provider} size={22}/><span><b>{p.name}</b><small>{p.model||t('未填写模型 ID')}</small></span>{data[kind==='chat'?'default_chat_id':'default_embedding_id']===p.id&&<Check size={15}/>}</button>)}<footer>{t('{0} 个配置',profiles.length)}<span>{t('最多 30 个')}</span></footer></aside>
            {current&&<form className="settings-model-form" onSubmit={e=>{e.preventDefault();run(save)}}><header className="settings-form-heading"><ProviderLogo provider={current.provider} size={34}/><div><h3>{current.name}{data[kind==='chat'?'default_chat_id':'default_embedding_id']===current.id&&<span>{t('默认')}</span>}</h3><p>{current.provider} · {current.model||t('未填写模型 ID')}</p></div><button type="button" className="secondary" disabled={busy||!current.model||!current.base_url} onClick={()=>run(probe)}>{t('测试连接')}</button></header><div className="settings-form-body">
              <section className="settings-form-section"><h4>{t('基础信息')}</h4><div className="fields"><label>{t('配置名称')}<input required maxLength={80} value={current.name} onChange={e=>edit({name:e.target.value})}/></label><label>{t('供应商')}<select value={current.provider} disabled={busy} onChange={e=>{const provider=data.providers.find(p=>p.id===e.target.value)!;const context=providerContextDefaults(provider.id);edit({provider:provider.id,base_url:provider.base_url,model:'',api_key:'',api_key_set:false,clear_key:false,pricing:undefined,input_modalities:['text'],...context})}}>{data.providers.filter(p=>p.kinds.includes(kind)).map(p=><option key={p.id} value={p.id}>{t(p.name)}</option>)}</select></label><label>{t('模型 ID')}<input required maxLength={200} value={current.model} placeholder={t('填写供应商提供的模型 ID')} onChange={e=>{const limit=knownModelContext(current.provider,e.target.value);edit({model:e.target.value,pricing:undefined,...(limit?{model_context_window:limit,context_window:Math.min(current.context_window||256_000,limit)}:{})})}}/></label><label>Base URL<input required type="url" value={current.base_url} onChange={e=>edit({base_url:e.target.value,api_key:'',api_key_set:false,pricing:undefined})}/></label></div><p className="field-hint">{current.provider==='anthropic'?t('使用 Anthropic 原生接口。'):t('使用 OpenAI 兼容接口；可按账号区域修改服务地址。')}</p><label>API Key<input type="password" autoComplete="new-password" value={current.api_key||''} placeholder={current.api_key_set?t('已保存，留空保留现有密钥'):t('本机服务可留空')} onChange={e=>edit({api_key:e.target.value,clear_key:false})}/></label>{current.api_key_set&&<label className="check-label settings-clear-key"><input type="checkbox" checked={!!current.clear_key} onChange={e=>edit({clear_key:e.target.checked})}/>{t('清除已保存的密钥')}</label>}</section>
              <label className="settings-default-row"><span><b>{t(kind==='chat'?'设为默认语言模型':'设为默认嵌入模型')}</b><small>{t(kind==='chat'?'新对话优先使用此配置，仍可在提问框切换。':'新的 LlamaIndex 知识库优先使用此配置。')}</small></span><input type="checkbox" checked={data[kind==='chat'?'default_chat_id':'default_embedding_id']===id} onChange={()=>setData({...data,[kind==='chat'?'default_chat_id':'default_embedding_id']:id})}/></label>
              {kind==='chat'&&<details className="settings-advanced"><summary><span>{t('高级设置')}<small>{t('上下文、输出和图片输入')}</small></span></summary><div className="settings-advanced-body"><div className="fields"><label>{t('请求超时（秒）')}<input type="number" required min={5} max={300} value={current.timeout} onChange={e=>edit({timeout:Number(e.target.value)})}/></label><label>{t('最大输出 Token')}<input type="number" required min={128} max={32768} value={current.max_tokens} onChange={e=>edit({max_tokens:Number(e.target.value)})}/></label><label>{t('模型上下文上限（Token）')}<input type="number" required min={Math.max(8192,current.max_tokens+4097,current.context_window||0)} max={2000000} value={current.model_context_window??1000000} onChange={e=>edit({model_context_window:Number(e.target.value)})}/></label><label>{t('工作上下文预算（Token）')}<input type="number" required min={Math.max(8192,current.max_tokens+4097)} max={current.model_context_window||2000000} value={current.context_window??256000} onChange={e=>edit({context_window:Number(e.target.value)})}/></label><label>{t('压缩触发比例')}<input type="number" required min={55} max={95} value={current.context_compaction_trigger??75} onChange={e=>edit({context_compaction_trigger:Number(e.target.value)})}/></label><label>{t('压缩后目标比例')}<input type="number" required min={25} max={80} value={current.context_compaction_target??50} onChange={e=>edit({context_compaction_target:Number(e.target.value)})}/></label></div><p className="field-hint">{t('达到触发比例后整理较早对话，并尽量回落到目标比例；目标必须低于触发比例。')}</p><fieldset className="model-input-capabilities"><legend>{t('输入能力')}</legend><label className="check-label"><input type="checkbox" checked disabled/>{t('文本')}</label><label className="check-label"><input type="checkbox" checked={(current.input_modalities||['text']).includes('image')} onChange={e=>edit({input_modalities:e.target.checked?['text','image']:['text']})}/>{t('图片')}</label></fieldset><div className="pricing-settings"><b>{t('费用价格')}</b>{current.pricing?.status==='official'?<><p>{t('自动使用供应商官方价格')} · {current.pricing.currency}{current.pricing.rate_tier&&<> · {t(current.pricing.rate_tier==='peak'?'峰时价格':current.pricing.rate_tier==='off_peak'?'非峰时价格':'标准价格')}</>}</p><p className="field-hint">{t('最近核验：{0}',current.pricing.verified_at||'—')} · <a href={current.pricing.source_url} target="_blank" rel="noreferrer">{t('查看官方价格')}</a></p></>:current.pricing?.status==='local'?<p>{t('本地模型不产生 API Token 费用，不包含设备与电力成本。')}</p>:<p>{t('当前模型或接口没有可可靠匹配的官方价格，回答中不会猜测费用。')}</p>}</div></div></details>}
              <p className="field-hint settings-key-note">{t('密钥仅保存在本机设置文件中，不会返回浏览器。更换供应商或地址后请重新填写密钥。')}</p>
            </div><footer className="settings-form-actions"><button className="primary" disabled={busy}>{t('保存更改')}</button><button type="button" className="settings-delete" disabled={busy||profiles.length<=1} onClick={()=>setDeleting(current)}><Trash2 size={15}/>{t('删除配置')}</button></footer></form>}
          </div>
        </section>}

        {section==='network'&&data.web_tools&&<WebToolsSettings initial={data.web_tools} onChanged={result=>{setData(v=>({...v,web_tools:result.web_tools}));onChanged(result)}}/>}
        {section==='experimental'&&data.decision_tools&&<DecisionToolsSettings initial={data.decision_tools} onChanged={result=>{setData(v=>({...v,decision_tools:result.decision_tools}));onChanged(result)}}/>}
        {section==='data'&&<ArchivedSettings onChanged={onArchiveChanged}/>}
      </div>
    </div>
    {deleting&&<div className="modal-backdrop"><section className="modal small-modal" role="dialog" aria-label={t('删除模型配置')}><h2>{t('删除模型配置')}</h2><p>{t('仅移除设置中的配置，不删除对话、知识库或已有索引。')}</p><b>{deleting.name}</b><div className="button-row"><button className="secondary" onClick={()=>setDeleting(null)}>{t('取消')}</button><button className="primary" onClick={()=>{const remaining=data.profiles.filter(p=>p.id!==deleting.id);const next=remaining.find(p=>p.kind===deleting.kind)!;const key=deleting.kind==='chat'?'default_chat_id':'default_embedding_id';setData({...data,profiles:remaining,[key]:data[key]===deleting.id?next.id:data[key]});setId(next.id);setDeleting(null)}}>{t('确认删除')}</button></div></section></div>}
  </main>;
}
