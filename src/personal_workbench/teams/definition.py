"""Bounded declarative team packages and untrusted model-generated task plans."""
from pydantic import Field, model_validator
from personal_workbench.workflows.definition import StrictModel, Expert, Node, WorkflowInput


class TeamInput(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default='', max_length=1000)
    experts: dict[str, Expert]
    planning: str = Field(min_length=1, max_length=4000)
    synthesis: str = Field(min_length=1, max_length=4000)
    max_tasks: int = Field(default=6, ge=1, le=6)
    concurrency: int = Field(default=2, ge=1, le=2)
    max_retries: int = Field(default=1, ge=0, le=1)
    task_timeout: int = Field(default=180, ge=10, le=600)
    max_model_calls: int = Field(default=32, ge=4, le=80)
    max_tokens: int = Field(default=200000, ge=10000, le=500000)

    @model_validator(mode='after')
    def valid(self):
        import re
        if not self.name.strip() or not 2 <= len(self.experts) <= 8 or 'leader' not in self.experts:
            raise ValueError('团队需要一位 leader 团长和 1–7 位成员。')
        if any(not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', key) for key in self.experts):
            raise ValueError('专家标识须使用小写字母、数字和下划线，以字母开头。')
        return self


class Task(StrictModel):
    id: str = Field(pattern=r'^[a-z][a-z0-9_]{0,31}$')
    name: str = Field(min_length=1, max_length=80)
    expert: str
    task: str = Field(min_length=1, max_length=4000)
    depends_on: list[str] = Field(default_factory=list, max_length=6)
    inputs: list[str] | None = Field(default=None, max_length=6)


class Plan(StrictModel):
    tasks: list[Task] = Field(min_length=1, max_length=6)


def compile_plan(raw, team):
    plan = Plan.model_validate(raw)
    if len(plan.tasks) > team['max_tasks']: raise ValueError('任务数量超过团队上限。')
    for task in plan.tasks:
        if task.id in ('plan', 'merge', 'deliver'): raise ValueError('任务使用了系统保留标识。')
        if task.expert == 'leader' or task.expert not in team['experts']: raise ValueError('任务引用了未授权成员。')
        # Model output cannot reach the runtime's private stages.
        if set(task.depends_on + (task.inputs or [])) - {t.id for t in plan.tasks}:
            raise ValueError('任务引用了不存在的前置成果。')
    nodes = [planning_node(team)]
    for task in plan.tasks:
        nodes.append(Node(**{**task.model_dump(), 'depends_on':task.depends_on or ['plan'],
                             'inputs':task.inputs if task.inputs is not None else task.depends_on,
                             'timeout':team['task_timeout']}).model_dump())
    ids = [t.id for t in plan.tasks]
    nodes.extend([
        Node(id='merge',name='团长汇总',expert='leader',task=team['synthesis'],depends_on=ids,inputs=ids,timeout=team['task_timeout']).model_dump(),
        Node(id='deliver',name='交付成果',kind='deliver',depends_on=['merge']).model_dump(),
    ])
    flow = WorkflowInput.model_validate({**{k:team[k] for k in ('name','description','experts','concurrency','max_model_calls','max_tokens')},'nodes':nodes})
    return plan.model_dump(), flow.model_dump()['nodes']


def planning_node(team):
    return Node(id='plan',name='团长制定计划',expert='leader',task='根据用户目标和团队规则生成任务计划。',timeout=team['task_timeout']).model_dump()


def templates():
    reads = ['list_files','search_files','read_file','read_outline','read_page']
    experts = {
        'leader': {'name':'研究团长','instructions':'负责拆解研究目标、核对证据和解决分歧。汇总时保留资料引用，明确证据不足与结论边界。','tool_ids':reads},
        'researcher': {'name':'资料研究员','instructions':'独立查阅用户选中的资料，提取支持与反对证据，记录可核对的引用。','tool_ids':reads},
        'analyst': {'name':'比较分析师','instructions':'根据已完成的研究成果比较方案，核查矛盾、条件与遗漏，不把假设写成事实。','tool_ids':reads},
    }
    compare = dict(name='方案比较团队',description='团长按问题制定研究计划，多位成员独立查证、比较，再汇总建议。',experts=experts,
                   planning='比较两种方案时，为两种方案分别安排独立研究任务，可复用 researcher 角色；再安排 analyst 分析任务，依赖两份研究结果。其他问题按实际需要拆解，不制造无意义任务。每项任务写清目标、所需证据和交付要求。',
                   synthesis='按“结论、证据对比、分歧与限制、建议”汇总所有任务成果。冲突以可核对证据为准，无法裁定时保留分歧，保留引用。')
    learning = dict(name='专题学习团队',description='围绕学习问题动态安排概念研究、难点分析和学习建议。',experts=experts,
                    planning='根据学习目标安排概念、示例或易错点研究；可并行的研究不要互相依赖。必要时安排 analyst 汇总前置研究。任务说明包含预期学习成果。',
                    synthesis='按“知识地图、关键解释、常见误区、练习建议”组织学习资料。保留证据引用，区分资料内容和补充解释；证据不一致时说明原因。')
    return [TeamInput.model_validate(t).model_dump() for t in (compare,learning)]
