"""A team-owned plan/validate/execute capability; platform routing remains generic.

Dynamic plans compile to the same bounded LangGraph wave executor used by fixed
packages. No generated code, recursive agents, or platform-level team routing.
"""
import hashlib
import json
from personal_workbench.capabilities import PreparedRun, RunStopped
from personal_workbench.teams.definition import Plan, compile_plan, planning_node
from personal_workbench.workflows.runtime import FixedWorkflowCapability
from personal_workbench.workflows.memory import ParentMemoryLearning


class TeamCapability(FixedWorkflowCapability):
    def __init__(self, base, store, partners, definition):
        super().__init__(base,store,partners,{**definition,'nodes':[planning_node(definition)]})

    def prepare(self, request, snapshot=None):
        if request.get('skill_refs'): raise ValueError('团队使用成员绑定的技能，请在团队配置中修改。')
        if request['resume'] and snapshot:
            snapshot = self.store.run(snapshot['run_id'])['snapshot']
            stages = self.store.stages(snapshot['run_id'])
            if any(s.get('exhausted') for s in stages.values()) and not any(s['status']=='waiting_approval' for s in stages.values()):
                raise ValueError('失败重试已达上限，请检查配置后新建对话。')
        return super().prepare(request,snapshot)

    def _stage_input(self, node, prepared, stages, stage):
        if node['id'] != 'plan': return super()._stage_input(node,prepared,stages,stage)
        team = prepared.snapshot['workflow']
        experts = prepared.snapshot['experts']
        text = json.dumps({'user_goal':self.store.run(prepared.snapshot['run_id'])['request']['text'],
                           'planning_rules':team['planning'],'max_tasks':team['max_tasks'],'validation_feedback':stage.get('error',''),
                           'members':{key:{'name':e['name'],'role':e['instructions'][:1200],'tools':e['tool_ids'],'connector_tools':', '.join(r.get('name',r['id']) for r in prepared.snapshot['connector_tools'] if r['id'] in e.get('connector_tool_ids',[]))[:1000]} for key,e in experts.items() if key!='leader'},
                           'output_schema':Plan.model_json_schema()},ensure_ascii=False)
        return text, []

    def _run_stage(self, node, stage, prepared, stop, emit, decision=None):
        rid = prepared.snapshot['run_id']; team = prepared.snapshot['workflow']
        if node['id']=='plan':
            leader = prepared.snapshot['experts']['leader']
            leader = {**leader,'instructions':leader['instructions']+'\n当前仅负责规划，不执行任务。仅输出符合给定 schema 的 JSON 对象，不加代码围栏。角色仅从 members 选择。depends_on 表示先后依赖；inputs 只能引用祖先任务。禁止循环。任务说明包含交付要求。',
                      'tool_ids':[],'connector_tool_ids':[],'skill_refs':[]}
            prepared = PreparedRun(prepared.request,{**prepared.snapshot,'kb_ids':[],'kb_id':None,'experts':{**prepared.snapshot['experts'],'leader':leader}},prepared.runtime)
        while True:
            stage = self.store.stages(rid)[node['id']]
            if stage.get('exhausted'): return
            if stop.is_set(): raise RunStopped()
            # Resume a checkpoint in place;
            # never recreate an Agent/receipt directory to retry a partially run tool.
            super()._run_stage(node,stage,prepared,stop,emit,decision)
            stage = self.store.stages(rid)[node['id']]
            if stage['status']!='failed':
                if stage['status']=='completed':
                    self.store.stage(rid,stage,artifact_version=1,output_hash=hashlib.sha256(stage['output'].encode()).hexdigest())
                return
            if stage.get('exhausted') or stop.is_set(): return
            emit('progress',label=node['name']+' · 失败重试')
            # Invalid JSON is a new leader turn in the same isolated checkpoint.
            # Transient model/tool errors resume existing checkpoints, preserving receipts.

    def execute(self, prepared, stop, emit):
        rid = prepared.snapshot['run_id']
        self.store.begin(prepared); self._parent_session(prepared,'running')
        ParentMemoryLearning(self.base).register(prepared)
        snap = self.store.run(rid)['snapshot']
        prepared = PreparedRun(prepared.request,snap,prepared.runtime)
        if not snap.get('plan'):
            self.store.update(rid,status='running')
            try:
                node = snap['workflow']['nodes'][0]
                stage = self.store.stages(rid)['plan']
                if stage['status']!='completed': self._run_stage(node,stage,prepared,stop,emit)
                stage = self.store.stages(rid)['plan']
                if stop.is_set(): raise RunStopped()
                if stage['status']!='completed':
                    status = stage['status']
                    self.store.update(rid,status=status)
                    memory_result=ParentMemoryLearning(self.base).finish(prepared)
                    self._parent_session(prepared,status)
                    return {'status':status,'thread_id':prepared.request['thread_id'],'output':'','memory_result':memory_result}
                raw = json.loads(stage['output'].strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip())
                plan,nodes = compile_plan(raw,snap['workflow'])
                snap = self.store.activate(prepared,plan,nodes)
                prepared = PreparedRun(prepared.request,snap,prepared.runtime)
                self.store.begin(prepared)
                emit('stage',node_id='plan',status='completed')
            except RunStopped:
                self.store.update(rid,status='stopped'); self._parent_session(prepared,'stopped'); raise
            except Exception:
                self.store.update(rid,status='failed'); self._parent_session(prepared,'failed'); raise
        return super().execute(prepared,stop,emit)

    def inspect(self, tid):
        result = super().inspect(tid)
        for run in result['workflow_runs']:
            saved = self.store.run(run['run_id'])
            run['plan'] = saved['snapshot'].get('plan')
            run['execution_mode'] = 'dynamic'
            states = self.store.stages(run['run_id'])
            for stage in run['stages']:
                node = next(n for n in run['nodes'] if n['id']==stage['node_id'])
                stage['blocked_by'] = [d for d in node['depends_on'] if states[d]['status'] not in ('completed','skipped')]
        if not result['pending'] and any(s.get('exhausted') for s in self.store.stages(result['workflow_runs'][-1]['run_id']).values()): result['next']=[]
        return result


def register_teams(jobs, store, partners):
    for team in store.list():
        for definition in team['revisions']:
            try: jobs.registry.resolve(definition['id'],definition['version'])
            except ValueError: jobs.registry.register(TeamCapability(jobs.assistant,store,partners,definition))
