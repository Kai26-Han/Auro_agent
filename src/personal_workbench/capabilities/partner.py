"""一个伙伴配置装配为单 Agent 能力；平台继续使用原统一协议。"""
from personal_workbench.capabilities.assistant import AssistantCapability


class PartnerCapability(AssistantCapability):
    def __init__(self, base, store, definition):
        self.base, self.store, self.partner_definition = base, store, definition
        self.id, self.version = definition['id'], definition['version']

    def __getattr__(self, name):
        return getattr(self.base, name)

    def describe(self):
        current = self.store.get(self.id)
        return {**self.partner_definition, 'enabled': current['enabled'], 'archived': current['archived']}

    def prepare(self, request, snapshot=None):
        request = dict(request)
        if not request['resume']:
            current = self.store.get(self.id)
            if not current['enabled'] or current['archived']: raise ValueError('此伙伴已停用或归档，请新建对话选择其他伙伴。')
            bound = [{**ref,'selection':'bound'} for ref in self.partner_definition['skill_refs']]
            if bound:
                requested=[{key:value for key,value in ref.items() if key!='selection'} for ref in request.get('skill_refs',[])]
                configured=[{key:value for key,value in ref.items() if key!='selection'} for ref in bound]
                if requested and requested != configured:
                    raise ValueError('伙伴已绑定技能，不能在会话中替换；请修改伙伴配置并新建对话。')
                request['skill_refs'] = bound
            if not request['existing'] and request.get('model_profile_id') is None:
                request['model_profile_id'] = self.partner_definition['model_profile_id']
        return super().prepare(request, snapshot)


def register_partners(jobs, store):
    for partner in store.list():
        for definition in partner['revisions']:
            try: jobs.registry.resolve(definition['id'],definition['version'])
            except ValueError: jobs.registry.register(PartnerCapability(jobs.assistant,store,definition))


def _issues(jobs, definition, available):
    reasons = []
    profile = definition['model_profile_id']
    if profile:
        try: jobs.app_settings.profile(profile)
        except ValueError: reasons.append('默认模型已移除，请修改配置或在对话中选择其他模型。')
    for ref in definition['skill_refs']:
        try:
            skill = jobs.assistant.skills.get(ref['id'])
            meta = jobs.assistant.skills.revision(ref['id'],ref['revision'])
            if not skill['enabled'] or skill['archived'] or not meta['compatible']:
                reasons.append('绑定技能已停用、归档或不兼容。')
            if set(meta['required_tools']) - set(definition['tool_ids']):
                reasons.append('助手未提供技能所需的必需工具。')
        except ValueError:
            reasons.append('绑定技能版本或文件不可用。')
    if any(tid not in available or available[tid]['policy']=='disabled' or available[tid]['availability']!='connected'
           for tid in definition.get('connector_tool_ids',[])):
        reasons.append('部分连接器工具未连接或未授权，使用前请检查连接器。')
    return reasons


def partner_catalog(jobs, store):
    connectors = jobs.assistant.connectors
    available = {t['id']:{'policy':t['policy'],'availability':c['status']}
                 for c in connectors.list(summary=True) for t in c['tools']} if connectors else {}
    builtin = jobs.assistant.describe()
    builtin_revisions = [{**definition, 'issues':_issues(jobs,definition,available)}
                         for definition in builtin.get('revisions',[])]
    if builtin_revisions:
        builtin = {**builtin, 'issues':_issues(jobs,builtin,available), 'revisions':builtin_revisions}
    result = [builtin]
    for partner in store.list():
        revisions = [{**definition, 'issues':_issues(jobs,definition,available)}
                     for definition in partner['revisions']]
        result.append({**partner, 'issues': revisions[0]['issues'], 'revisions': revisions})
    return result
