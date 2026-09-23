import { useEffect, useState } from 'react';
import { api } from './api';
import { t } from './i18n';
import type { PartnerDefinition } from './PartnerCatalog';

type Preview = {stages:{id:string;name:string;expert:string;tool_ids:string[]}[]};
const labels:Record<string,string>={list_files:'查看资料目录',search_files:'检索资料',read_file:'阅读资料片段',read_outline:'查看文档目录',read_page:'阅读文档页面',create_note:'准备笔记草稿',web_search:'网页搜索',web_fetch:'读取网页',paper_search:'论文搜索',jev_decide:'Jev 决策测试'};

/** Uses the same authorization preparation as a real run; never starts a job. */
export function WorkflowToolPreview({partner,kbIds,connectorIds,modelId,locked,onConfigure}:{
  partner:PartnerDefinition;kbIds:string[];connectorIds:string[];modelId:string;locked:boolean;onConfigure:()=>void;
}) {
  const [state,setState] = useState<{key:string;data?:Preview;error?:string}|null>(null);
  const key=JSON.stringify([partner.id,partner.version,kbIds,connectorIds,modelId]);
  useEffect(()=>{
    if(locked)return;
    let cancelled=false;
    const timer=setTimeout(()=>{
      const [id,version,kids,connectors,model]=JSON.parse(key);
      api<Preview>('/workflows/'+id+'/tool-preview',{method:'POST',body:JSON.stringify({text:'tool-preview',capability_version:version,kb_ids:kids,connector_tool_ids:connectors,model_profile_id:model||null})})
        .then(data=>{if(!cancelled)setState({key,data});})
        .catch(e=>{if(!cancelled)setState({key,error:e.message});});
    },150);
    return()=>{cancelled=true;clearTimeout(timer);};
  },[key,locked]);
  const current=state?.key===key?state:null;
  const empty=current?.data?.stages.filter(s=>!s.tool_ids.length).length||0;
  if(locked)return null;
  return <div className="workflow-tool-preview">
    {!kbIds.length&&partner.tool_ids.some(id=>['list_files','search_files','read_file','read_outline','read_page'].includes(id))&&<p className="workflow-warning">{t('尚未选择知识库，流程无法读取你的资料。请在提问框「＋ → 知识库」中选择。')}</p>}
    {current?.error?<p className="workflow-warning" role="status">{t(current.error)}</p>:current?.data?<details>
      <summary>{t('本轮各阶段工具')} · {empty?t('{0} 个阶段无可用工具',empty):t('各阶段均有可用工具')}</summary>
      <div className="workflow-tool-list">{current.data.stages.map(s=><p key={s.id}><b>{s.name} · {s.expert}</b><br/><span title={s.tool_ids.join(', ')}>{s.tool_ids.map(id=>t(labels[id]||id)).join('、')||t('无可用工具，仅处理用户输入和前置成果')}</span></p>)}
      <p>{t('可用工具由成员授权、知识库选择和工具开关共同决定；联网工具需在成员配置中勾选。')}</p>
      <button type="button" className="skill-text-button" onClick={onConfigure}>{t('配置流程成员权限')}</button>
      </div>
    </details>:<p>{t('正在检查流程工具…')}</p>}
  </div>;
}
