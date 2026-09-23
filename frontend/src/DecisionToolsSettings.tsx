import { useState } from 'react';
import { FlaskConical } from 'lucide-react';
import { api } from './api';
import { t } from './i18n';
import type { Preferences } from './SettingsPage';

export type DecisionToolsConfig={jev_decide:boolean;model:string;api_key?:string;api_key_set?:boolean;clear_key?:boolean;timeout:number;max_state_chars:number};

export function DecisionToolsSettings({initial,onChanged}:{initial:DecisionToolsConfig;onChanged:(v:Preferences)=>void}) {
  const [data,setData]=useState(initial);const [busy,setBusy]=useState(false);const [error,setError]=useState('');const [notice,setNotice]=useState('');const [saved,setSaved]=useState(false);
  const edit=(patch:Partial<DecisionToolsConfig>)=>{setData(v=>({...v,...patch}));setSaved(false);setNotice('');};
  async function save(e:React.FormEvent){e.preventDefault();setBusy(true);setError('');setSaved(false);try{
    const {api_key_set:_private,...input}=data;
    const result=await api<Preferences>('/settings/decision-tools',{method:'PUT',body:JSON.stringify(input)});
    setData(result.decision_tools!);onChanged(result);setSaved(true);
  }catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  async function probe(){setBusy(true);setError('');setNotice('');try{
    const {api_key_set:_private,...input}=data;
    const result=await api<{model:string;answer:number}>('/settings/decision-tools/probe',{method:'POST',body:JSON.stringify(input)});
    setNotice(t('连接成功，Jev 已返回测试判断（模型：{0}）。',result.model));
  }catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  return <section className="settings-models web-tool-settings"><h2><FlaskConical size={20}/> {t('实验性决策工具')}</h2>
    <p className="field-hint">{t('Jev 只做结构化判断，不生成回答或执行操作。启用后仍需在伙伴或流程成员中授权，模型才可以按需调用。')}</p>
    <form onSubmit={save}><fieldset disabled={busy} style={{border:0,padding:0,margin:0}}>
      <div className="web-tool-options"><label><input type="checkbox" checked={data.jev_decide} onChange={e=>edit({jev_decide:e.target.checked})}/><FlaskConical size={19}/><span><b>Jev</b><small>jev_decide · {t('Choice、Score、Noul 决策测试')}</small></span></label></div>
      <div className="settings-model-form"><div className="fields"><label>{t('模型 ID')}<input required maxLength={84} value={data.model} onChange={e=>edit({model:e.target.value})}/></label><label>{t('请求超时（秒）')}<input required type="number" min={5} max={60} value={data.timeout} onChange={e=>edit({timeout:Number(e.target.value)})}/></label></div>
      <label>TypeSafe API Key<input type="password" autoComplete="new-password" value={data.api_key||''} placeholder={data.api_key_set?t('已保存，留空保留现有密钥'):t('启用 Jev 前填写密钥')} onChange={e=>edit({api_key:e.target.value,clear_key:false})}/></label>
      {data.api_key_set&&<label className="check-label"><input type="checkbox" checked={!!data.clear_key} onChange={e=>edit({clear_key:e.target.checked})}/>{t('清除已保存的密钥')}</label>}
      <label>{t('单次状态字符上限')}<input required type="number" min={1000} max={16000} step={1000} value={data.max_state_chars} onChange={e=>edit({max_state_chars:Number(e.target.value)})}/></label>
      <p className="field-hint">{t('调用时，工具参数中的 state、问题和候选标准会发送给 TypeSafe。请只提供判断所需的最少信息，不发送完整私人文档或敏感凭据。')}</p>
      <p className="field-hint">{t('Jev 的概率和置信度只是参考信号，不能扩大权限、跳过确认或替代证据。')}</p>
      <p className="field-hint">{t('测试连接会向 TypeSafe 发送固定测试命题“1 + 1 = 2”，不会发送你的对话或资料。')}</p>
      {error&&<p className="knowledge-error" role="alert">{error}</p>}{(saved||notice)&&<p className="knowledge-notice" role="status">{notice||t('Jev 设置已保存，下次提问生效。')}</p>}
      <div className="button-row"><button className="primary" disabled={busy}>{busy?t('正在保存…'):t('保存 Jev 设置')}</button><button type="button" className="secondary" disabled={busy||(!data.api_key&&!data.api_key_set)} onClick={probe}>{t('测试 Jev 连接')}</button></div></div>
    </fieldset></form>
  </section>;
}
