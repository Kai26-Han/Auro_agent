"""Same durable execution records, separate immutable expert-team packages."""
import hashlib
import json
from personal_workbench.workflows.store import WorkflowStore, encode


class TeamStore(WorkflowStore):
    filename = 'teams.sqlite'
    source = 'team'
    execution_mode = 'dynamic'

    def activate(self, prepared, plan, nodes):
        rid = prepared.snapshot['run_id']
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            snap = json.loads(db.execute('SELECT snapshot FROM runs WHERE id=?',(rid,)).fetchone()[0])
            if not snap.get('plan'):
                snap['plan'] = {'version':1,'hash':hashlib.sha256(encode(plan).encode()).hexdigest(),**plan}
                snap['workflow'] = {**snap['workflow'],'nodes':nodes}
                db.execute('UPDATE runs SET snapshot=? WHERE id=?',(encode(snap),rid))
                for node in nodes:
                    data = {'node_id':node['id'],'name':node['name'],'kind':node['kind'],'expert':node['expert'],
                            'expert_name':snap['experts'].get(node['expert'],{}).get('name',''),
                            'attempt':1,'status':'pending','output':'','sources':[],'records':[],'error':'','approved':None}
                    db.execute('INSERT OR IGNORE INTO stages VALUES (?,?,?,?)',(rid,node['id'],1,encode(data)))
            # The saved plan is the authority after any replay or restart.
        return snap

    def stage(self, rid, node, **changes):
        from personal_workbench.teams.definition import compile_plan
        from personal_workbench.workflows.store import now
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM stages WHERE run_id=? AND node_id=? AND attempt=?',
                             (rid,node['node_id'],node['attempt'])).fetchone()
            current = json.loads(row[0]) if row else node
            # An already committed result is immutable; replay/late writes cannot
            # replace it. Metadata enrichments may not change its payload or status.
            if current['status']=='completed':
                if changes.get('status','completed')!='completed' or changes.get('output',current['output'])!=current['output']:
                    return current
            data = {**current,**changes,'updated':now()}
            team = json.loads(db.execute('SELECT snapshot FROM runs WHERE id=?',(rid,)).fetchone()[0])['workflow']
            if data['node_id']=='plan' and changes.get('status')=='completed':
                try:
                    raw = json.loads(data['output'].strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip())
                    compile_plan(raw,team)
                except (ValueError,TypeError):
                    data.update(status='failed',retry_review=True,error='计划校验未通过：请检查任务数量、成员、依赖和输入引用。')
            if data['status']=='failed' and current['status']!='failed':
                data['failures'] = current.get('failures',[]) + [{'time':now(),'error':data.get('error','')}]
                data['exhausted'] = len(data['failures']) > team['max_retries']
            db.execute('INSERT INTO stages VALUES (?,?,?,?) ON CONFLICT(run_id,node_id,attempt) DO UPDATE SET data=excluded.data',
                       (rid,data['node_id'],data['attempt'],encode(data)))
            return data
