"""S6 reviewed imports, portable manifests and immutable-version reuse."""
import hashlib
import json
import re
import sqlite3
import time
from typing import Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field
from personal_workbench.partner_store import PartnerInput
from personal_workbench.workflows.definition import WorkflowInput
from personal_workbench.teams.definition import TeamInput
from personal_workbench.file_tools import TOOL_INFO
from personal_workbench.capabilities.partner import register_partners
from personal_workbench.workflows.runtime import register_workflows
from personal_workbench.teams.runtime import register_teams

Kind = Literal['assistant','workflow','team']
SCHEMAS = {'assistant':PartnerInput,'workflow':WorkflowInput,'team':TeamInput}


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Bundle(Strict):
    format: Literal['personal-workbench.capability'] = 'personal-workbench.capability'
    schema_version: Literal[1] = 1
    kind: Kind
    definition: dict
    origin: dict[str,str] = Field(default_factory=dict,max_length=3)
    dependencies: list[dict] = Field(default_factory=list,max_length=1000)
    source: dict[str,str] = Field(default_factory=dict,max_length=3)


class Preview(Strict):
    kind: Kind
    definition: dict
    source_preview_id: str | None = Field(default=None,pattern=r'^[a-f0-9]{32}$')
    target_id: str | None = Field(default=None,max_length=80)


class Import(Strict):
    package: Bundle
    target_id: str | None = Field(default=None,max_length=80)


class Commit(Strict):
    preview_id: str = Field(pattern=r'^[a-f0-9]{32}$')


class Suggest(Strict):
    text: str = Field(min_length=2,max_length=10000)
    thread_id: str | None = Field(default=None,max_length=80)
    selected_id: str | None = Field(default=None,max_length=80)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


class CapabilityStudio:
    def __init__(self,jobs,partners,workflows,teams,preferences,knowledge,connectors):
        self.jobs,self.stores,self.preferences,self.knowledge,self.connectors = jobs,{'assistant':partners,'workflow':workflows,'team':teams},preferences,knowledge,connectors
        self.skills=jobs.assistant.skills
        self.path=jobs.settings.data_dir/'capability-studio.sqlite'
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS previews(id TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL, result TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS origins(id TEXT, version TEXT, metadata TEXT, PRIMARY KEY(id,version))')

    def definition(self,kind,value):
        try: data=SCHEMAS[kind].model_validate(value).model_dump()
        except ValueError: raise ValueError('配置结构无效，请检查角色、流程依赖和执行上限。') from None
        experts=[data] if kind=='assistant' else list(data['experts'].values())
        for e in experts:
            if not e.get('partner_id') and not e['instructions'].strip():raise ValueError('角色说明不能为空。')
            if set(e['tool_ids'])-set(TOOL_INFO):raise ValueError('配置引用了未知工具。')
        if not data['name'].strip():raise ValueError('名称不能为空。')
        if kind=='assistant' and any(not q.strip() or len(q)>1000 for q in data['examples']):raise ValueError('示例问题须为 1–1000 个字符。')
        return data

    def resources(self,kind,data):
        """Revalidate current dependencies on preview AND commit, never grant access."""
        dependencies=[];issues=[]
        experts=[data] if kind=='assistant' else list(data['experts'].values())
        for expert in experts:
            e=expert
            if e.get('partner_id'):
                try:
                    partner=self.stores['assistant'].get(e['partner_id'])
                    e=next(v for v in partner['revisions'] if v['version']==e['partner_version'])
                    if partner['archived'] or not partner['enabled']:raise ValueError()
                    dependencies.append({'kind':'assistant','name':e['name'],'id':e['id'],'version':e['version']})
                except (ValueError,StopIteration):issues.append('引用的助手版本缺失或不可用。');continue
            for ref in e.get('skill_refs',[]):
                try:
                    skill=self.skills.get(ref['id']);meta=self.skills.revision(ref['id'],ref['revision'])
                    if skill['archived'] or not skill['enabled'] or not meta['compatible']:raise ValueError()
                    dependencies.append({'kind':'skill','name':skill['display_name'],**ref})
                except ValueError:issues.append('引用的技能版本缺失或不可用，请先安装技能或调整引用。')
            ids={t['id']:t for t in self.connectors.catalog()}
            for tid in e.get('connector_tool_ids',[]):
                if tid not in ids:issues.append('连接器工具缺失，请先连接服务或调整工具引用。')
                elif ids[tid]['policy']=='disabled':issues.append('连接器工具尚未授权，请在连接器中配置后重新预览。')
                else:dependencies.append({'kind':'connector_tool','id':tid,'name':ids[tid].get('name',tid)})
        if kind=='assistant':
            if data['model_profile_id']:
                try:self.preferences.profile(data['model_profile_id'])
                except ValueError:issues.append('默认模型配置不存在，请改为跟随默认模型或选择本机已有配置。')
            for kid in data['suggested_kb_ids']:
                try:self.knowledge.row(kid)
                except ValueError:issues.append('推荐知识库不存在，请调整知识库引用。')
        return dependencies,list(dict.fromkeys(issues))

    def preview(self,body):
        data=self.definition(body.kind,body.definition)
        dependencies,issues=self.resources(body.kind,data)
        current=self.stores[body.kind].get(body.target_id) if body.target_id else None
        old=self.definition(body.kind,{k:current[k] for k in SCHEMAS[body.kind].model_fields if k in current}) if current else {}
        changes=[{'field':k,'before':old.get(k),'after':v} for k,v in data.items() if old.get(k)!=v]
        experts=[data] if body.kind=='assistant' else list(data['experts'].values())
        # Include permissions from pinned assistant references in the review.
        resolved=[]
        for e in experts:
            if e.get('partner_id'):
                try:e=next(v for v in self.stores['assistant'].get(e['partner_id'])['revisions'] if v['version']==e['partner_version'])
                except (ValueError,StopIteration):pass
            resolved.append(e)
        result={'preview_id':uuid4().hex,'kind':body.kind,'definition':data,'target_id':body.target_id,
                'expected_version':current['version'] if current else None,'changes':changes,
                'dependencies':dependencies,'issues':issues,
                'permissions':{'tools':sorted({t for e in resolved for t in e.get('tool_ids',[])}),
                               'connectors':sorted({t for e in resolved for t in e.get('connector_tool_ids',[])})}}
        if body.source_preview_id:
            with sqlite3.connect(self.path) as db:
                row=db.execute('SELECT data,created FROM previews WHERE id=?',(body.source_preview_id,)).fetchone()
            if not row or row[1]<time.time()-3600:raise ValueError('配置预览已过期，请重新预览。')
            source=json.loads(row[0])
            if source['kind']!=body.kind:raise ValueError('能力类型与导入来源不一致。')
            for key in ('import_origin','import_dependencies'):
                if key in source:result[key]=source[key]
        with sqlite3.connect(self.path) as db:
            db.execute('DELETE FROM previews WHERE created<?',(time.time()-3600,))
            db.execute('INSERT INTO previews VALUES (?,?,?,NULL)',(result['preview_id'],json.dumps(result,ensure_ascii=False),time.time()))
        return result

    def commit(self,token):
        with self.jobs.lock,sqlite3.connect(self.path) as db:
            row=db.execute('SELECT data,created,result FROM previews WHERE id=?',(token,)).fetchone()
            if not row or row[1]<time.time()-3600:raise ValueError('配置预览已过期，请重新预览。')
            if row[2]:return json.loads(row[2])
            p=json.loads(row[0]);store=self.stores[p['kind']]
            if p['target_id'] and store.get(p['target_id'])['version']!=p['expected_version']:
                raise ValueError('配置已有更新，请重新预览差异，避免覆盖新版本。')
            _,issues=self.resources(p['kind'],p['definition'])
            if issues:raise ValueError('；'.join(issues))
            result=store.save(SCHEMAS[p['kind']].model_validate(p['definition']),p['target_id'])
            if p['kind']=='assistant':register_partners(self.jobs,store)
            elif p['kind']=='workflow':register_workflows(self.jobs,store,self.stores['assistant'])
            else:register_teams(self.jobs,store,self.stores['assistant'])
            public={k:result[k] for k in ('id','version','version_number','name')}
            if p.get('import_origin'):
                db.execute('INSERT OR IGNORE INTO origins VALUES (?,?,?)',(result['id'],result['version'],json.dumps({'origin':p['import_origin'],'dependencies':p.get('import_dependencies',[])},ensure_ascii=False)))
            db.execute('UPDATE previews SET result=? WHERE id=?',(json.dumps(public,ensure_ascii=False),token))
            return public

    def export(self,kind,identifier,version=None):
        current=self.stores[kind].get(identifier)
        selected=next((v for v in current['revisions'] if v['version']==(version or current['version'])),None)
        if not selected:raise ValueError('版本不存在。')
        data=self.definition(kind,{k:selected[k] for k in SCHEMAS[kind].model_fields if k in selected})
        deps,_=self.resources(kind,data)
        # Freeze referenced assistant roles into the portable package. Skills stay
        # immutable content-addressed references, not arbitrary executable uploads.
        if kind!='assistant':
            for key,e in list(data['experts'].items()):
                if e.get('partner_id'):
                    p=self.stores['assistant'].get(e['partner_id'])
                    v=next(v for v in p['revisions'] if v['version']==e['partner_version'])
                    data['experts'][key]={k:v.get(k,[]) for k in ('name','instructions','skill_refs','tool_ids','connector_tool_ids')}
        with sqlite3.connect(self.path) as db: row=db.execute('SELECT metadata FROM origins WHERE id=? AND version=?',(identifier,selected['version'])).fetchone()
        provenance=json.loads(row[0]) if row else {}
        deps=list({json.dumps(d,sort_keys=True):d for d in deps+provenance.get('dependencies',[])}.values())
        return Bundle(kind=kind,definition=data,origin={'id':identifier,'version':selected['version'],'digest':fingerprint(data)},dependencies=deps,source=provenance.get('origin',{})).model_dump()

    def import_package(self,body):
        bundle=body.package
        if len(bundle.model_dump_json().encode())>2*1024*1024:raise ValueError('能力包最多 2 MiB。')
        if bundle.origin.get('digest') and bundle.origin['digest']!=fingerprint(bundle.definition):
            raise ValueError('能力包内容与摘要不一致，请检查文件。')
        data=self.definition(bundle.kind,bundle.definition)
        # Across workspaces Skill IDs differ; only identical content hashes may map.
        experts=[data] if bundle.kind=='assistant' else list(data['experts'].values())
        for e in experts:
            for ref in e.get('skill_refs',[]):
                try:self.skills.revision(ref['id'],ref['revision'])
                except ValueError:
                    found=[s for s in self.skills.list() if any(r['revision']==ref['revision'] for r in s['revisions'])]
                    if len(found)==1:ref['id']=found[0]['id']
        result=self.preview(Preview(kind=bundle.kind,definition=data,target_id=body.target_id))
        result.update(import_origin=bundle.source or bundle.origin,import_dependencies=bundle.dependencies)
        with sqlite3.connect(self.path) as db:db.execute('UPDATE previews SET data=? WHERE id=?',(json.dumps(result,ensure_ascii=False),result['preview_id']))
        return result

    def revisions(self,kind,identifier):
        item=self.stores[kind].get(identifier)
        return [{'version':v['version'],'version_number':v['version_number'],'definition':{k:v[k] for k in SCHEMAS[kind].model_fields if k in v}} for v in item['revisions']]

    def suggestions(self,body):
        # Explicit choice/bound conversations always win. No execution side effect.
        if body.selected_id and body.selected_id!='workbench-assistant':return []
        if body.thread_id:
            with self.jobs.connect() as db:
                if db.execute('SELECT 1 FROM bindings WHERE thread_id=?',(body.thread_id,)).fetchone():return []
        def tokens(text):
            text=text.lower();words=set(re.findall(r'[a-z0-9]{3,}',text))
            for chunk in re.findall(r'[\u4e00-\u9fff]+',text):words.update(chunk[i:i+2] for i in range(len(chunk)-1))
            return words-{'请帮','帮我','一下','问题','完成','任务','进行','可以','一个','我的','需要'}
        wanted=tokens(body.text);scored=[]
        for kind,store in self.stores.items():
            for item in store.list():
                if not item['enabled'] or item['archived']:continue
                overlap=wanted & tokens(item['name']+' '+item['description'])
                if len(overlap)>=2:scored.append({'id':item['id'],'version':item['version'],'name':item['name'],'kind':kind,'matches':sorted(overlap)[:6],'score':len(overlap)})
        return sorted(scored,key=lambda x:(-x['score'],x['name']))[:3]
