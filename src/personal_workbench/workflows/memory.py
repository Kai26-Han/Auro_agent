"""Parent-task memory boundary for fixed workflows and dynamic teams.

The parent goal is the only text eligible for semantic/procedural learning.  A
verified delivery receipt is the only child-produced evidence eligible for an
episode/event.  Plans, member messages, review drafts and the delivered prose
never cross this boundary.
"""
import json
import sqlite3
from pathlib import PurePosixPath

from langchain_core.messages import ToolMessage


def delivery_receipt(artifact):
    """Return a receipt-shaped tool message without copying artifact content."""
    if not isinstance(artifact, str) or PurePosixPath(artifact).name != artifact or '\\' in artifact:
        return []
    return [ToolMessage(
        content=json.dumps({'saved': artifact}, ensure_ascii=False),
        tool_call_id='parent-work-delivery', name='create_note', status='success')]


def parent_outcome(root, thread_id, run_id):
    """Read the durable parent result used by crash recovery.

    A result is accepted only when the parent completed and its delivery did
    not retain unresolved review findings.
    """
    for filename in ('workflows.sqlite', 'teams.sqlite'):
        path = root / filename
        if not path.exists():
            continue
        try:
            with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
                db.row_factory = sqlite3.Row
                row = db.execute(
                    'SELECT status,quality,calls,tokens,snapshot FROM runs WHERE thread_id=? AND id=?',
                    (thread_id, run_id)).fetchone()
                if row is None:
                    continue
                artifact = None
                stages = [json.loads(stage['data']) for stage in db.execute(
                    'SELECT data FROM stages WHERE run_id=? ORDER BY rowid DESC', (run_id,)).fetchall()]
                for data in stages:
                    if data.get('kind') == 'deliver' and data.get('status') == 'completed':
                        # The receipt is eligible only when delivery committed
                        # a regular file and recorded its digest and size.
                        if isinstance(data.get('artifact_sha256'), str) and len(data['artifact_sha256']) == 64 and isinstance(data.get('artifact_bytes'), int):
                            artifact = data.get('artifact')
                        break
                accepted = row['status'] == 'completed' and row['quality'] != 'needs_review'
                # Current budget limits cannot resume. Keep only the narrow
                # one-time compatibility retry for runs created with the old
                # byte estimator. An exhausted dynamic plan is also final.
                snap = json.loads(row['snapshot'])
                limited = [stage for stage in stages if stage.get('status') == 'limited']
                legacy_retry = (row['status'] == 'limited' and
                    row['calls'] < snap['workflow']['max_model_calls'] and
                    row['tokens'] < snap['workflow']['max_tokens'] and limited and
                    all(not stage.get('budget') and stage.get('error') == '流程总预算已用尽，请缩小任务范围后新建对话。' for stage in limited))
                terminal = (row['status'] in ('completed', 'rejected', 'conflict') or
                            (row['status'] == 'limited' and not legacy_retry) or
                            (row['status'] == 'failed' and any(stage.get('exhausted') for stage in stages)))
                return {'status': row['status'], 'quality': row['quality'],
                        'accepted': accepted, 'terminal': terminal, 'artifact': artifact}
        except (sqlite3.Error, ValueError, TypeError):
            # A partially initialized database is not evidence of completion.
            continue
    return None


class ParentMemoryLearning:
    """Idempotently bind one native memory operation to one parent run."""
    def __init__(self, base):
        self.base = base

    @staticmethod
    def frozen(prepared):
        return prepared.snapshot.get('memory_learning') or {}

    def register(self, prepared):
        frozen = self.frozen(prepared)
        req = prepared.request
        goal = prepared.snapshot.get('parent_goal', req.get('text', ''))
        if not frozen or not frozen.get('learn_memories'):
            return {'status': 'disabled'}
        try:
            if frozen.get('engine') == 'langmem':
                queued = self.base.learning.enqueue(
                    frozen, goal, req['thread_id'], prepared.snapshot['run_id'], await_answer=True)
                episode_id = self.base.memory.episodes.begin(
                    frozen, goal, req['thread_id'], prepared.snapshot['run_id'])
                return {**queued, 'episode_id': episode_id}
            if frozen.get('engine') == 'mem0' and self.base.memory.native_mem0.ready:
                return self.base.memory.native_mem0.enqueue(
                    frozen, goal, req['thread_id'], prepared.snapshot['run_id'], await_answer=True)
            return {'status': 'compat'}
        except Exception as exc:
            # Memory is secondary: an unavailable memory engine must not fail
            # the user's workflow or team task.
            return {'status': 'failed', 'reason': type(exc).__name__}

    def finish(self, prepared):
        frozen = self.frozen(prepared)
        if not frozen or not frozen.get('learn_memories'):
            return {'status': 'disabled'}
        req, run = prepared.request, prepared.snapshot['run_id']
        goal = prepared.snapshot.get('parent_goal', req.get('text', ''))
        outcome = parent_outcome(self.base.settings.data_dir, req['thread_id'], run)
        if not outcome or not outcome['terminal']:
            return {'status': 'awaiting_answer'}
        messages = delivery_receipt(outcome['artifact']) if outcome['accepted'] else []
        try:
            if frozen.get('engine') == 'langmem':
                queued = self.base.learning.enqueue(frozen, goal, req['thread_id'], run, await_answer=True)
                episode_id = self.base.memory.episodes.begin(frozen, goal, req['thread_id'], run)
                if outcome['accepted'] and episode_id:
                    self.base.memory.episodes.observe(episode_id, messages)
                if outcome['accepted']:
                    self.base.memory.episodes.finish(episode_id, 'completed')
                else:
                    self.base.memory.episodes.cancel(episode_id, 'parent_not_accepted')
                state = self.base.learning.release(queued['job_id'], outcome['accepted']) if queued.get('job_id') else queued.get('status', 'skipped')
                return {'status': 'queued' if state == 'pending' else state, 'engine': 'langmem'}
            if frozen.get('engine') == 'mem0' and self.base.memory.native_mem0.ready:
                queued = self.base.memory.native_mem0.enqueue(frozen, goal, req['thread_id'], run, await_answer=True)
                if not queued.get('operation_id'):
                    return queued
                result = self.base.memory.native_mem0.release(
                    queued['operation_id'], completed=outcome['accepted'], messages=messages)
                return {**result, 'status': 'queued' if result['status'] == 'pending' else result['status']}
            if outcome['accepted']:
                return self.base.memory.learn(frozen, goal, req['thread_id'], run)
            return {'status': 'cancelled'}
        except Exception as exc:
            return {'status': 'failed', 'reason': type(exc).__name__}
