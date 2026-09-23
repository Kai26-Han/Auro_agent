"""The fixed-workflow package language. No executable code is accepted."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from personal_workbench.file_tools import TOOL_INFO
from personal_workbench.partner_store import BoundSkill
from personal_workbench.skill_runtime import MAX_SKILLS_PER_ACTOR


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Expert(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    instructions: str = Field(default='', max_length=12000)
    partner_id: str | None = None
    partner_version: str | None = None
    tool_ids: list[str] = Field(default_factory=list, max_length=len(TOOL_INFO))
    connector_tool_ids: list[str] = Field(default_factory=list, max_length=100)
    skill_refs: list[BoundSkill] = Field(default_factory=list, max_length=MAX_SKILLS_PER_ACTOR)

    @model_validator(mode='after')
    def valid(self):
        if bool(self.partner_id) != bool(self.partner_version):
            raise ValueError('伙伴引用必须同时提供 ID 和版本。')
        if not self.partner_id and not self.instructions.strip():
            raise ValueError('请填写专家角色说明。')
        if set(self.tool_ids) - set(TOOL_INFO): raise ValueError('流程专家包含未知工具。')
        return self


class Condition(StrictModel):
    node: str
    approved: bool


class Node(StrictModel):
    id: str = Field(pattern=r'^[a-z][a-z0-9_]{0,31}$')
    name: str = Field(min_length=1, max_length=80)
    kind: Literal['agent', 'review', 'deliver'] = 'agent'
    expert: str | None = None
    task: str = Field(default='', max_length=4000)
    depends_on: list[str] = Field(default_factory=list, max_length=12)
    inputs: list[str] | None = Field(default=None, max_length=12)
    when: Condition | None = None
    rework_target: str | None = None
    max_rework: int = Field(default=0, ge=0, le=1)
    timeout: int = Field(default=180, ge=10, le=600)


class WorkflowInput(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default='', max_length=1000)
    experts: dict[str, Expert]
    nodes: list[Node] = Field(min_length=2, max_length=12)
    concurrency: int = Field(default=2, ge=1, le=2)
    max_model_calls: int = Field(default=32, ge=4, le=80)
    max_tokens: int = Field(default=150000, ge=10000, le=500000)

    @model_validator(mode='after')
    def valid(self):
        if not self.name.strip() or not 1 <= len(self.experts) <= 8: raise ValueError('请输入名称，并配置 1–8 位专家。')
        nodes = {n.id: n for n in self.nodes}
        if len(nodes) != len(self.nodes): raise ValueError('阶段 ID 不能重复。')
        order, remaining = [], set(nodes)
        while remaining:
            ready = [key for key in remaining if set(nodes[key].depends_on) <= set(order)]
            if not ready: raise ValueError('阶段依赖不存在或形成循环。')
            order.extend(ready); remaining.difference_update(ready)
        ancestors = {}
        for key in order:
            node = nodes[key]
            ancestors[key] = set(node.depends_on) | set().union(*(ancestors[d] for d in node.depends_on))
            if node.inputs is not None and set(node.inputs) - ancestors[key]: raise ValueError('输入成果必须来自前置阶段。')
            if node.kind != 'deliver' and node.expert not in self.experts: raise ValueError('阶段引用的专家不存在。')
            if node.when and (node.when.node not in ancestors[key] or nodes[node.when.node].kind != 'review'):
                raise ValueError('条件只能引用前置审校阶段。')
            if node.max_rework:
                if node.kind != 'review' or node.rework_target not in ancestors[key]: raise ValueError('返工必须回到审校之前的阶段。')
                if nodes[node.rework_target].when or nodes[node.rework_target].kind != 'agent': raise ValueError('返工目标必须为无条件的专家阶段。')
            elif node.rework_target: raise ValueError('设置返工目标时须允许一次返工。')
        deliveries = [n for n in self.nodes if n.kind == 'deliver']
        if len(deliveries) != 1 or deliveries[0].when or ancestors[deliveries[0].id] != set(nodes)-{deliveries[0].id}:
            raise ValueError('请设置唯一最终交付阶段，汇合所有阶段，且不设置条件。')
        return self


def templates():
    reads = ['list_files', 'search_files', 'read_file', 'read_outline', 'read_page']
    experts = {
        'researcher': {'name':'资料研究员','instructions':'查阅用户选中的资料，提炼事实、来源、限制；找不到证据就明确说明。','tool_ids':reads},
        'writer': {'name':'报告作者','instructions':'基于前置研究成果组织清晰的报告，区分资料事实和建议，保留引用，不编造依据。','tool_ids':reads},
        'reviewer': {'name':'审校员','instructions':'独立核对事实、逻辑和引用，指出具体问题和修改建议。','tool_ids':reads},
    }
    report = {'name':'资料报告流程','description':'资料整理、写作、审校与交付；审校未通过时允许一次返工。','experts':experts,'nodes':[
        {'id':'research','name':'资料整理','expert':'researcher','task':'围绕用户目标查阅资料，交付带引用的事实清单。'},
        {'id':'write','name':'撰写报告','expert':'writer','depends_on':['research'],'task':'根据资料整理结果撰写报告；若有返工意见，逐项修正。'},
        {'id':'review','name':'独立审校','kind':'review','expert':'reviewer','depends_on':['write'],'inputs':['research','write'],'task':'审查报告是否满足目标、事实是否有依据、引用是否正确。','rework_target':'write','max_rework':1},
        {'id':'deliver','name':'交付报告','kind':'deliver','depends_on':['review'],'inputs':['write']},
    ]}
    checks = {'name':'双人并行检查','description':'两位专家分别核对事实和结构，汇合后形成检查建议。','experts':experts,'nodes':[
        {'id':'facts','name':'事实检查','expert':'researcher','task':'独立检查用户目标涉及的事实与证据，保留来源。'},
        {'id':'structure','name':'结构检查','expert':'reviewer','task':'独立检查目标的逻辑、结构、遗漏和需要澄清的问题。'},
        {'id':'merge','name':'汇总建议','expert':'writer','depends_on':['facts','structure'],'task':'读取两份独立检查，整理共同结论、分歧和下一步建议，不把意见当作事实。'},
        {'id':'deliver','name':'交付检查结果','kind':'deliver','depends_on':['merge']},
    ]}
    return [WorkflowInput.model_validate(item).model_dump() for item in (report, checks)]
