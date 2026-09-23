import { useState } from 'react';
import { Globe, Search, FileText, GraduationCap } from 'lucide-react';
import { api } from './api';
import { t } from './i18n';
import type { Preferences } from './SettingsPage';

export type WebToolsConfig = { web_search:boolean; web_fetch:boolean; paper_search:boolean; provider:'bing'|'duckduckgo'|'tavily'|'brave'|'searxng'; api_key?:string; api_key_set?:boolean; clear_key?:boolean; base_url:string; max_results:number; timeout:number; max_chars:number };
export function WebToolsSettings({ initial, onChanged }: {initial:WebToolsConfig;onChanged:(v:Preferences)=>void}) {
  const [data,setData]=useState(initial);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [saved,setSaved]=useState(false);
  const edit=(patch:Partial<WebToolsConfig>)=>{setData(v=>({...v,...patch}));setSaved(false);};
  async function save(e:React.FormEvent) {
    e.preventDefault();setBusy(true);setError('');setSaved(false);
    try {
      const {api_key_set:_private,...input}=data;
      const result=await api<Preferences>('/settings/web-tools',{method:'PUT',body:JSON.stringify(input)});
      setData(result.web_tools!);onChanged(result);setSaved(true);
    } catch(e) {setError((e as Error).message);} finally {setBusy(false);}
  }
  return <section className="settings-models web-tool-settings"><h2><Globe size={20}/> {t('联网工具')}</h2><p className="field-hint">{t('启用后，助手可按问题访问公开网络，也可结合所选知识库回答。明确要求“仅根据知识库”时不联网。')}</p>
    <form onSubmit={save}><fieldset disabled={busy} style={{border:0,padding:0,margin:0}}>
      <div className="web-tool-options">
        <label><input type="checkbox" checked={data.web_search} onChange={e=>edit({web_search:e.target.checked})}/><Search size={19}/><span><b>{t('网页搜索')}</b><small>web_search · {t('搜索标题、摘要和来源链接')}</small></span></label>
        <label><input type="checkbox" checked={data.web_fetch} onChange={e=>edit({web_fetch:e.target.checked})}/><FileText size={19}/><span><b>{t('网页读取')}</b><small>web_fetch · {t('读取公开网页正文，无需密钥')}</small></span></label>
        <label><input type="checkbox" checked={data.paper_search} onChange={e=>edit({paper_search:e.target.checked})}/><GraduationCap size={19}/><span><b>{t('论文搜索')}</b><small>paper_search · {t('arXiv 预印本与摘要，无需密钥')}</small></span></label>
      </div>
      <div className="settings-model-form"><div className="fields"><label>{t('网页搜索供应商')}<select value={data.provider} onChange={e=>edit({provider:e.target.value as WebToolsConfig['provider'],api_key:'',api_key_set:false,clear_key:false,base_url:''})}><option value="bing">Bing · {t('无需密钥')}</option><option value="duckduckgo">DuckDuckGo · {t('无需密钥')}</option><option value="tavily">Tavily</option><option value="brave">Brave</option><option value="searxng">SearXNG</option></select></label><label>{t('搜索结果上限')}<input required type="number" min={1} max={10} value={data.max_results} onChange={e=>edit({max_results:Number(e.target.value)})}/></label></div>
      {data.provider==='searxng' && <label>{t('SearXNG 服务地址')}<input type="url" required={data.web_search} value={data.base_url} placeholder="http://127.0.0.1:8080" onChange={e=>edit({base_url:e.target.value})}/><small>{t('服务需启用 JSON 搜索结果。')}</small></label>}
      {(data.provider==='tavily'||data.provider==='brave')&&<><label>API Key<input type="password" autoComplete="new-password" value={data.api_key||''} placeholder={data.api_key_set?t('已保存，留空保留现有密钥'):t('填写搜索服务密钥')} onChange={e=>edit({api_key:e.target.value,clear_key:false})}/></label>{data.api_key_set&&<label className="check-label"><input type="checkbox" checked={!!data.clear_key} onChange={e=>edit({clear_key:e.target.checked})}/>{t('清除已保存的密钥')}</label>}</>}
      <div className="fields"><label>{t('请求超时（秒）')}<input type="number" required min={5} max={60} value={data.timeout} onChange={e=>edit({timeout:Number(e.target.value)})}/></label><label>{t('网页正文字符上限')}<input type="number" required min={1000} max={50000} step={1000} value={data.max_chars} onChange={e=>edit({max_chars:Number(e.target.value)})}/></label></div>
      <p className="field-hint">{t('搜索会将查询词发送给所选服务。网页读取不支持登录、动态页面或 PDF；论文搜索返回摘要，不下载全文。')}</p><p className="field-hint">{t('技能和伙伴仍受各自的工具权限限制。现有伙伴如需联网，请在伙伴配置中勾选相应工具。')}</p>
      {error&&<p className="knowledge-error" role="alert">{error}</p>}{saved&&<p className="knowledge-notice" role="status">{t('联网设置已保存，下次提问生效。')}</p>}<button className="primary" disabled={busy}>{busy?t('正在保存…'):t('保存联网设置')}</button></div>
    </fieldset></form></section>;
}
