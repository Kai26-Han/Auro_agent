"""后台执行一次任务，持久化进度供 SSE 重连；服务重启后只标记中断，不自动重跑。"""

import json
import logging
import sqlite3
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from datetime import datetime, timezone
from personal_workbench.capabilities import Registry, PreparedRun, DEFAULT_CAPABILITY, DEFAULT_VERSION, RunStopped
from personal_workbench.capabilities.assistant import AssistantCapability
from personal_workbench.capabilities.protocol import (
    PROTOCOL_VERSION, RUN_STATUSES, normalize_event, normalize_prepared_request, normalize_request,
    normalize_result, normalize_snapshot,
)
from personal_workbench.observability import TraceStore, attempt_span_id
from personal_workbench.runtime_limits import automatic_chat_capacity, execution_lane, lane_limit, queue_reason
from personal_workbench.pricing import cost_metrics, resolve_pricing

logger = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _seconds(start, end):
    try:
        return max(0, round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()))
    except (TypeError, ValueError):
        return 0


def public_job(row):
    result = dict(row)
    cid = result.get('capability_id') or DEFAULT_CAPABILITY
    version = result.get('capability_version') or DEFAULT_VERSION
    result['capability_id'], result['capability_version'] = cid, version
    snapshot = json.loads(result['snapshot']) if result.get('snapshot') else None
    result['snapshot'] = normalize_snapshot(snapshot, cid, version, result['id']) if snapshot else None
    value = json.loads(result['result']) if result.get('result') else None
    result['result'] = normalize_result(value, status=result['status'], thread_id=result['thread_id']) if value else None
    result['protocol_version'] = PROTOCOL_VERSION
    result['trace_id'] = result.get('trace_id') or (snapshot or {}).get('run_id') or result['id']
    return result


class Jobs:
    def __init__(self, settings, library, model_override=None, knowledge=None, registry=None):
        self.settings, self.library = settings, library
        self.knowledge = knowledge
        self.assistant = AssistantCapability(settings, library, knowledge, model_override)
        self.registry = registry or Registry([self.assistant])
        self.path = settings.data_dir / "web.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Revision closes one turn and starts its replacement under one host
        # lifecycle lock. RLock lets revise() reuse start() without a race.
        self.lock = threading.RLock()
        # The UI submits tasks; this internal scheduler decides when they run.
        # Remote requests may overlap, while a local model is kept serial to
        # avoid competing Ollama/Metal contexts. Queued local work never takes
        # a worker away from a runnable remote task.
        self.capacity = automatic_chat_capacity()
        self.pool = ThreadPoolExecutor(max_workers=self.capacity, thread_name_prefix="workbench")
        self.active_runs = {}
        self.thread_runs = {}
        self.pending_runs = []
        self.running_by_lane = Counter()
        self.active = None
        self.stop_event = None
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, thread_id TEXT, status TEXT, created TEXT, error TEXT, protocol_version INTEGER NOT NULL DEFAULT 1, trace_id TEXT, resource_class TEXT, queue_reason TEXT, started_at TEXT, ended_at TEXT);
                CREATE TABLE IF NOT EXISTS bindings (
                    thread_id TEXT PRIMARY KEY, capability_id TEXT NOT NULL, capability_version TEXT NOT NULL, snapshot TEXT
                );
                CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, data TEXT);
                CREATE TABLE IF NOT EXISTS turn_revisions (
                    thread_id TEXT NOT NULL,
                    original_run_id TEXT NOT NULL,
                    revised_run_id TEXT NOT NULL,
                    created TEXT NOT NULL,
                    PRIMARY KEY(thread_id, original_run_id)
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            for name in ("capability_id", "capability_version", "snapshot", "result", "protocol_version", "trace_id", "resource_class", "queue_reason", "started_at", "ended_at"):
                if name not in columns:
                    kind = "INTEGER NOT NULL DEFAULT 1" if name == "protocol_version" else "TEXT"
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
            db.execute("UPDATE jobs SET trace_id=COALESCE(trace_id,id)")
            db.execute("UPDATE jobs SET status='interrupted', error='服务已重启，请从会话中继续任务。', ended_at=COALESCE(ended_at,?) WHERE status IN ('running','queued','stopping')", (now(),))
        self.traces = TraceStore(self.path)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        # connections used below are explicitly closed by contextlib.closing in db helper
        return ClosingConnection(db)

    def get(self, jid):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            if not row:
                raise ValueError("任务不存在。")
            return public_job(row)

    def list(self):
        with self.connect() as db:
            return [public_job(row) for row in db.execute("SELECT * FROM jobs ORDER BY created DESC, rowid DESC LIMIT 30")]

    def active_jobs(self):
        with self.lock:
            ids = list(self.active_runs)
        return [self.get(jid) for jid in ids]

    def active_for_thread(self, thread_id):
        with self.lock:
            jid = self.thread_runs.get(thread_id)
        return self.get(jid) if jid else None

    def _active_placeholder(self, thread_id):
        with self.lock:
            jid = self.thread_runs.get(thread_id)
            entry = self.active_runs.get(jid) if jid else None
            if not entry:
                return None
            prepared = entry['prepared']
            request, snapshot = prepared.request, prepared.snapshot
        job = self.get(jid)
        text = request.get('text') or '继续任务'
        return {
            'id':thread_id, 'title':text[:80], 'status':job['status'], 'mode':'agent', 'updated':job['created'],
            'project_id':request.get('project_id'), 'pinned':False, 'pinned_at':None,
            'archived':False, 'archived_at':None,
            'messages':([{'id':'pending-user-'+jid,'role':'human','content':text,
                          'run_id':snapshot.get('run_id'),'skill_refs':snapshot.get('skill_refs',[])}]
                        if request.get('text') else []),
            'pending':[], 'next':[], 'sources':[], 'usage':{}, 'library_mode':snapshot.get('library_mode',0),
            'kb_id':snapshot.get('kb_id'), 'kb_ids':snapshot.get('kb_ids'),
            'model_profile_id':snapshot.get('model_profile_id'), 'skill_refs':snapshot.get('skill_refs',[]),
            'partner':snapshot.get('partner'), 'assistant_profile':snapshot.get('assistant_profile'),
            'connector_tool_ids':[ref['id'] for ref in snapshot.get('connector_tools',[])],
            'connector_runs':[], 'skill_runs':[], 'effective_tool_ids':snapshot.get('tool_ids',[]),
            'memory':snapshot.get('memory'), 'memory_binding':(snapshot.get('memory') or {}).get('memory_profile_id'),
            'working_memory':None, 'editable_turn':None, 'turn_revisions':[], 'legacy_mixed_memory':False,
            'reasoning_runs':{},
        }

    def session_summaries(self, stored):
        """Keep newly queued conversations navigable before their first worker starts."""
        by_id = {item['id']:dict(item) for item in stored}
        with self.lock:
            tids = list(self.thread_runs)
        for tid in tids:
            active = self.active_for_thread(tid)
            if tid in by_id:
                by_id[tid]['status'] = active['status']
            else:
                placeholder = self._active_placeholder(tid)
                if placeholder:
                    by_id[tid] = {key:placeholder[key] for key in ('id','title','status','mode','updated')}
        return sorted(by_id.values(), key=lambda item:item.get('updated',''), reverse=True)

    def _refresh_compat_active(self):
        self.active = next(iter(self.active_runs), None)
        self.stop_event = self.active_runs.get(self.active, {}).get('stop') if self.active else None

    def _queued_reason(self, lane):
        return queue_reason(lane, self.running_by_lane[lane] >= lane_limit(lane, self.capacity))

    def _refresh_queue_reasons_locked(self):
        with self.connect() as db:
            for jid in self.pending_runs:
                entry = self.active_runs.get(jid)
                if entry:
                    db.execute("UPDATE jobs SET queue_reason=? WHERE id=?", (self._queued_reason(entry['lane']), jid))

    def _dispatch_locked(self):
        """Admit the oldest runnable tasks without letting one busy lane block another."""
        while sum(self.running_by_lane.values()) < self.capacity:
            selected = next((index for index, jid in enumerate(self.pending_runs)
                             if (entry := self.active_runs.get(jid))
                             and self.running_by_lane[entry['lane']] < lane_limit(entry['lane'], self.capacity)), None)
            if selected is None:
                break
            jid = self.pending_runs.pop(selected)
            entry = self.active_runs[jid]
            entry['state'] = 'running'
            self.running_by_lane[entry['lane']] += 1
            with self.connect() as db:
                db.execute("UPDATE jobs SET status='running', queue_reason=NULL, started_at=COALESCE(started_at,?) WHERE id=?", (now(), jid))
            future = self.pool.submit(self.run, jid, entry['thread_id'], entry['capability'], entry['prepared'], entry['stop'])
            future.add_done_callback(lambda completed, job_id=jid, resource_lane=entry['lane']:
                                     self._worker_returned(job_id, resource_lane, completed))
        self._refresh_queue_reasons_locked()

    def _worker_returned(self, jid, lane, future):
        """Recover capacity if a worker exits before the normal finalizer runs."""
        with self.lock:
            entry = self.active_runs.get(jid)
            if not entry or entry.get('state') != 'running':
                return
            message = "任务执行器意外结束，请从会话中重试。"
            with self.connect() as db:
                db.execute("UPDATE jobs SET status='failed', error=?, queue_reason=NULL, ended_at=? WHERE id=?", (message, now(), jid))
            prepared, capability = entry['prepared'], entry['capability']
            self.traces.finish_job({"id":jid,"trace_id":prepared.snapshot['run_id'],
                                    "run_id":prepared.snapshot['run_id'],"thread_id":entry['thread_id'],
                                    "capability_id":capability.id,"capability_version":capability.version,
                                    "resumed":prepared.request['resume']},'failed')
            self.emit(jid, 'problem', message=message,
                      error_type=type(future.exception()).__name__ if future.exception() else 'WorkerExit')
            self.emit(jid, 'done', status='failed', thread_id=entry['thread_id'])
            self.active_runs.pop(jid, None)
            if self.thread_runs.get(entry['thread_id']) == jid:
                self.thread_runs.pop(entry['thread_id'], None)
            self.running_by_lane[lane] -= 1
            if self.running_by_lane[lane] <= 0:
                self.running_by_lane.pop(lane, None)
            self._refresh_compat_active()
            self._dispatch_locked()

    def events(self, jid, after=0):
        with self.connect() as db:
            return [{"id": row["seq"], **normalize_event(json.loads(row["data"]))} for row in db.execute(
                "SELECT seq, data FROM events WHERE job_id=? AND seq>? ORDER BY seq", (jid, after))]

    def trace_context(self, jid):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?",(jid,)).fetchone()
        if row is None: raise ValueError("任务不存在。")
        job = public_job(row)
        return {"trace_id":job["trace_id"],"run_id":job["snapshot"]["run_id"],"thread_id":job["thread_id"],
                "capability_id":job["capability_id"],"capability_version":job["capability_version"],"job_id":jid}

    def emit(self, jid, kind, **data):
        context = self.trace_context(jid)
        if kind == "trace_span":
            self.traces.record({**context, **data})
        event = normalize_event({"kind": kind, "trace_id":context["trace_id"],
                                 "span_id":data.get("span_id") or attempt_span_id(jid), **data}, emitted_at=now())
        with self.connect() as db:
            db.execute("INSERT INTO events(job_id, data) VALUES (?, ?)", (jid, json.dumps(event, ensure_ascii=False)))

    def trace(self, jid):
        job = self.get(jid)
        try:
            return self.traces.trace(job["trace_id"])
        except ValueError:
            # Pre-observability jobs receive a truthful minimal topology. Their
            # historical model/tool details were never recorded and are not invented.
            run_id=(job.get('snapshot') or {}).get('run_id') or job['trace_id']
            self.traces.begin_job(trace_id=job['trace_id'],job_id=job['id'],run_id=run_id,thread_id=job['thread_id'],
                                  capability_id=job['capability_id'],capability_version=job['capability_version'],resumed=False)
            self.traces.finish_job({"id":job['id'],"trace_id":job['trace_id'],"run_id":run_id,"thread_id":job['thread_id'],
                                    "capability_id":job['capability_id'],"capability_version":job['capability_version'],"resumed":False},job['status'])
            return self.traces.trace(job["trace_id"])

    def reasoning(self, run_id):
        """Build a short, public execution summary from durable observed events."""
        with self.connect() as db:
            jobs = [dict(row) for row in db.execute(
                "SELECT * FROM jobs WHERE trace_id=? ORDER BY rowid", (run_id,)).fetchall()]
            if not jobs:
                return None
            event_rows = db.execute(
                "SELECT e.job_id,e.data FROM events e JOIN jobs j ON j.id=e.job_id WHERE j.trace_id=? ORDER BY e.seq",
                (run_id,),
            ).fetchall()
        events_by_job = {job['id']: [] for job in jobs}
        events = []
        for row in event_rows:
            event = json.loads(row['data'])
            events.append(event)
            events_by_job.setdefault(row['job_id'], []).append(event)
        latest = jobs[-1]
        try:
            trace = self.traces.trace(run_id)
            spans = trace['spans']
        except ValueError:
            spans = []
        names = {str(span.get('name') or '') for span in spans}
        web_tools = {'web_search','web_fetch','paper_search'}
        has_web = bool(names & web_tools)
        has_retrieval = has_web or any(span.get('span_type') == 'retrieval' for span in spans)
        has_stages = any(event.get('kind') == 'stage' for event in events) or any(
            span.get('span_type') == 'stage' for span in spans)
        mode = 'executing' if has_stages else 'exploring' if has_retrieval else 'thinking'
        snapshot = json.loads(latest.get('snapshot') or '{}')
        if mode == 'executing':
            first = '正在分析任务并安排执行步骤'
        elif snapshot.get('kb_ids'):
            first = '正在分析问题并检索所选知识库'
        elif has_web:
            first = '正在分析问题并查找相关网络资料'
        else:
            first = '正在分析问题并组织回答'
        skip = ('排队', '队列', '记忆已进入', '本轮记忆', '父任务记忆', '正在准备会话上下文')
        mapping = {
            '已收到模型回复':'模型已完成本轮分析',
            '已完成工具调用':'已完成所需工具调用',
            '已处理保存决定':'已处理文件保存决定',
            '已处理文件保存':'已完成文件保存',
            '处理工具请求':'正在调用所需工具',
        }
        observed = []
        for event in events:
            if event.get('kind') != 'progress':
                continue
            label = str(event.get('label') or '').strip()
            if not label or any(part in label for part in skip):
                continue
            label = mapping.get(label, label)
            if label not in observed:
                observed.append(label[:160])
        source_count = sum(int(span.get('attributes', {}).get('source_count') or 0)
                           for span in spans if span.get('span_type') == 'retrieval')
        if source_count:
            observed.append('已取得相关资料')
        terminal = {
            'completed':'已完成分析并形成回答', 'waiting_approval':'正在等待你确认操作',
            'failed':'执行未完成，可以查看错误后重试', 'stopped':'任务已停止，当前进度已保留',
            'interrupted':'服务中断，当前进度已保留', 'limited':'本轮已达到运行上限',
            'rejected':'操作已取消', 'conflict':'操作需要进一步核对',
        }.get(latest['status'], '正在处理')
        middle = [item for item in observed if item not in {first, terminal}]
        steps = [first, *middle[:3]]
        if terminal not in steps:
            steps.append(terminal)
        durations = []
        for row in jobs:
            own = events_by_job.get(row['id'], [])
            done = next((event.get('emitted_at') for event in reversed(own)
                         if event.get('kind') == 'done'), None)
            durations.append(_seconds(row.get('started_at') or row['created'], row.get('ended_at') or done or now()))
        usage = {}
        try:
            usage = trace.get('usage', {})
        except (NameError, AttributeError):
            pass
        model = snapshot.get('model', {})
        pricing = snapshot.get('pricing') or model.get('pricing') or {}
        metrics = {**cost_metrics(usage, pricing), 'duration_seconds':sum(durations)}
        return {'run_id':run_id, 'status':latest['status'], 'mode':mode,
                'duration_seconds':sum(durations), 'steps':steps[:5], 'attempts':len(jobs),
                'metrics':metrics}

    def set_status(self, jid, status, error=None):
        if status not in RUN_STATUSES:
            raise ValueError("任务状态不符合运行协议。")
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=?, error=? WHERE id=?", (status, error, jid))

    @property
    def model_override(self):
        return self.assistant.model_override

    @model_override.setter
    def model_override(self, value):
        self.assistant.model_override = value

    @property
    def app_settings(self):
        return self.assistant.app_settings

    @app_settings.setter
    def app_settings(self, value):
        self.assistant.app_settings = value

    def binding(self, thread_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM bindings WHERE thread_id=?", (thread_id,)).fetchone()
        return dict(row) if row else {"capability_id": DEFAULT_CAPABILITY, "capability_version": DEFAULT_VERSION, "snapshot": None}

    def inspect(self, thread_id):
        binding = self.binding(thread_id)
        try:
            result = self.registry.resolve(binding["capability_id"], binding["capability_version"]).inspect(thread_id)
        except ValueError:
            placeholder = self._active_placeholder(thread_id)
            if placeholder:
                return placeholder
            raise
        with self.connect() as db:
            revisions = [dict(row) for row in db.execute(
                "SELECT original_run_id,revised_run_id,created FROM turn_revisions WHERE thread_id=? ORDER BY created",
                (thread_id,))]
        by_original = {row["original_run_id"]: row for row in revisions}
        by_revised = {row["revised_run_id"]: row for row in revisions}
        messages = []
        for message in result.get("messages", []):
            item = dict(message)
            run_id = item.get("run_id")
            if run_id in by_original:
                item.update(turn_state="superseded", revised_by_run_id=by_original[run_id]["revised_run_id"])
            elif run_id in by_revised:
                item.update(turn_state="revision", revises_run_id=by_revised[run_id]["original_run_id"])
            messages.append(item)
        result["messages"] = messages
        editable = None
        if result.get("next"):
            editable = next(({
                "message_id": message.get("id"), "run_id": message.get("run_id"),
                "content": message.get("content", ""), "attachments":message.get("attachments", [])
            } for message in reversed(messages)
                if message.get("role") == "human" and message.get("turn_state") != "superseded"), None)
        result["editable_turn"] = editable
        result["turn_revisions"] = revisions
        run_ids = {
            message.get('run_id') for message in messages
            if message.get('role') == 'ai' and message.get('run_id')
        }
        result['reasoning_runs'] = {
            run_id: summary for run_id in run_ids
            if (summary := self.reasoning(run_id)) is not None
        }
        return {**result, "capability_id": binding["capability_id"], "capability_version": binding["capability_version"]}

    def start(self, text=None, thread_id=None, resume=False, decision=None, kb_id=None, model_profile_id=None, kb_ids=None,
              capability_id=None, capability_version=None, skill_refs=None, connector_tool_ids=None, memory=None,
              revises_run_id=None, project_id=None, attachment_ids=None):
        with self.lock:
            tid, jid = thread_id or uuid4().hex[:12], uuid4().hex
            # A manually injected legacy busy marker is still treated as a
            # global guard.  Real runs only block another run in the same chat.
            if self.active and not self.active_runs:
                raise ValueError("已有任务运行中，请等待完成或先停止它。")
            if tid in self.thread_runs:
                raise ValueError("当前会话已有任务运行中，请等待完成或先停止它。")
            if resume and not thread_id:
                raise ValueError("恢复任务需要会话 ID。")
            binding = self.binding(tid)
            cid = capability_id or binding["capability_id"]
            version = capability_version or binding["capability_version"]
            if thread_id and (cid, version) != (binding["capability_id"], binding["capability_version"]):
                raise ValueError("会话绑定的伙伴不能切换，请新建对话。")
            capability = self.registry.resolve(cid, version)
            previous = json.loads(binding["snapshot"]) if resume and binding["snapshot"] else None
            request = normalize_request({"text": text, "thread_id": tid, "existing": bool(thread_id),
                "resume": resume, "decision": decision, "kb_id": kb_id, "kb_ids": kb_ids,
                "model_profile_id": model_profile_id, "skill_refs":skill_refs or [], "connector_tool_ids":connector_tool_ids or [], "memory":memory,
                "attachment_ids":attachment_ids or [],
                "project_id":project_id,
                "run_id":previous.get("run_id",jid) if previous else jid, "revises_run_id":revises_run_id})
            prepared = capability.prepare(request, previous)
            prepared_request = normalize_prepared_request(
                prepared.request, thread_id=tid, run_id=request['run_id'])
            frozen_snapshot = normalize_snapshot(prepared.snapshot, cid, version, prepared_request['run_id'])
            if prepared_request.get('attachment_ids'):
                frozen_snapshot['attachments'] = self.assistant.attachments.bind(
                    prepared_request['attachment_ids'], tid, prepared_request['run_id'])
            if (not resume and self.app_settings and not frozen_snapshot.get('pricing')
                    and prepared_request.get('model_profile_id')):
                try:
                    profile = self.app_settings.profile(prepared_request['model_profile_id'])
                    frozen_snapshot['pricing'] = resolve_pricing(profile, now())
                except ValueError:
                    pass
            prepared = PreparedRun(prepared_request, frozen_snapshot, prepared.runtime)
            snapshot = json.dumps(prepared.snapshot, ensure_ascii=False)
            trace_id = prepared.snapshot['run_id']
            # Validate and freeze the replacement before ending the old turn.
            # Configuration errors therefore leave the resumable checkpoint intact.
            if revises_run_id:
                supersede = getattr(capability, "supersede", capability.abandon)
                supersede(tid, revises_run_id)
            with self.connect() as db:
                lane = execution_lane(prepared.snapshot)
                db.execute("INSERT INTO jobs(id,thread_id,status,created,capability_id,capability_version,snapshot,protocol_version,trace_id,resource_class,queue_reason) VALUES (?,?,'queued',?,?,?,?,?,?,?,?)",
                           (jid, tid, now(), cid, version, snapshot, PROTOCOL_VERSION, trace_id, lane, queue_reason(lane, False)))
                db.execute("INSERT INTO bindings VALUES (?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET snapshot=excluded.snapshot",
                           (tid, cid, version, snapshot))
                if revises_run_id:
                    db.execute("INSERT INTO turn_revisions(thread_id,original_run_id,revised_run_id,created) VALUES (?,?,?,?)",
                               (tid, revises_run_id, prepared.snapshot['run_id'], now()))
            self.traces.begin_job(trace_id=trace_id,job_id=jid,run_id=prepared.snapshot['run_id'],thread_id=tid,
                                  capability_id=cid,capability_version=version,resumed=resume)
            stop = threading.Event()
            self.active_runs[jid] = {'thread_id': tid, 'stop': stop, 'state': 'queued', 'lane': lane,
                                     'capability': capability, 'prepared': prepared}
            self.thread_runs[tid] = jid
            self.pending_runs.append(jid)
            self._refresh_compat_active()
            self._dispatch_locked()
            job = self.get(jid)
            if job['status'] == 'queued':
                self.emit(jid, 'progress', label=job['queue_reason'])
            return job

    def cancel(self, jid):
        with self.lock:
            active = self.active_runs.get(jid)
            if not active:
                raise ValueError("这个任务已结束，无需停止。")
            if active['state'] == 'queued':
                self.pending_runs.remove(jid)
                with self.connect() as db:
                    db.execute("UPDATE jobs SET status='stopped', queue_reason=NULL, ended_at=? WHERE id=?", (now(), jid))
                self.traces.finish_job({"id":jid,"trace_id":active['prepared'].snapshot['run_id'],
                                        "run_id":active['prepared'].snapshot['run_id'],"thread_id":active['thread_id'],
                                        "capability_id":active['capability'].id,"capability_version":active['capability'].version,
                                        "resumed":active['prepared'].request['resume']},'stopped')
                self.emit(jid, 'done', status='stopped', thread_id=active['thread_id'])
                self.active_runs.pop(jid, None)
                if self.thread_runs.get(active['thread_id']) == jid:
                    self.thread_runs.pop(active['thread_id'], None)
                self._refresh_compat_active()
                self._dispatch_locked()
                return
            # 先记录请求，再通知执行线程；否则执行线程可能先写 stopped，
            # 随后被这里的 stopping 覆盖，留下无法恢复的错误展示。
            self.set_status(jid, "stopping")
            self.emit(jid, "progress", label="将在当前步骤完成后停止")
            active['stop'].set()

    def abandon(self, thread_id):
        with self.lock:
            if thread_id in self.thread_runs:
                raise ValueError("当前任务仍在运行，请先停止并等待结束。")
            binding = self.binding(thread_id)
            capability = self.registry.resolve(binding["capability_id"], binding["capability_version"])
            return capability.abandon(thread_id)

    def revise(self, thread_id, text, kb_id=None, model_profile_id=None, kb_ids=None,
               skill_refs=None, connector_tool_ids=None, memory=None, attachment_ids=None):
        """Replace the latest resumable turn while preserving its raw history."""
        with self.lock:
            if thread_id in self.thread_runs:
                raise ValueError("当前任务仍在运行，请先停止并等待结束。")
            binding = self.binding(thread_id)
            capability = self.registry.resolve(binding["capability_id"], binding["capability_version"])
            current = capability.inspect(thread_id)
            if not current.get("next"):
                raise ValueError("当前没有可编辑的已停止任务。")
            original = next((message for message in reversed(current.get("messages", []))
                             if message.get("role") == "human"), None)
            if not original or not original.get("run_id"):
                raise ValueError("无法识别需要编辑的原始提问。")
            original_run_id = original["run_id"]
            job = self.start(
                text=text, thread_id=thread_id, kb_id=kb_id,
                model_profile_id=model_profile_id, kb_ids=kb_ids,
                capability_id=binding["capability_id"], capability_version=binding["capability_version"],
                skill_refs=skill_refs, connector_tool_ids=connector_tool_ids, memory=memory,
                attachment_ids=attachment_ids,
                revises_run_id=original_run_id,
            )
            return {**job, "revises_run_id": original_run_id}

    @staticmethod
    def _memory_selection(snapshot):
        memory = snapshot.get('memory') or {}
        return memory.get('selection') or {
            key: memory[key] for key in ('space_id', 'use_memories', 'learn_memories') if key in memory
        } or None

    def regenerate(self, thread_id, message_id):
        """Regenerate the latest answer in place; older answers start a branch."""
        with self.lock:
            if thread_id in self.thread_runs:
                raise ValueError("请等待当前任务完成或先停止它。")
            binding = self.binding(thread_id)
            capability = self.registry.resolve(binding["capability_id"], binding["capability_version"])
            current = self.inspect(thread_id)
            target = next((message for message in current.get('messages', [])
                           if message.get('id') == message_id and message.get('role') == 'ai'), None)
            if not target or not target.get('run_id'):
                raise ValueError("回答不存在或已经删除。")
            run_id = target['run_id']
            prompt_message = next((message for message in current.get('messages', [])
                                   if message.get('role') == 'human' and message.get('run_id') == run_id), None)
            prompt = (prompt_message or {}).get('content', '')
            if not prompt:
                raise ValueError("无法找到这条回答对应的提问。")
            with self.connect() as db:
                row = db.execute("SELECT snapshot FROM jobs WHERE trace_id=? ORDER BY rowid DESC LIMIT 1", (run_id,)).fetchone()
            if not row or not row['snapshot']:
                raise ValueError("这条历史回答缺少运行快照，无法重新生成。")
            snapshot = json.loads(row['snapshot'])
            visible_ai = [message for message in current.get('messages', [])
                          if message.get('role') == 'ai' and message.get('turn_state') != 'superseded']
            in_place = bool(visible_ai and visible_ai[-1].get('id') == message_id)
            attachment_ids = [item['id'] for item in (prompt_message or {}).get('attachments', [])]
            if attachment_ids and not in_place:
                attachment_ids = self.assistant.attachments.clone(attachment_ids)
            kwargs = dict(text=prompt,
                          thread_id=thread_id if in_place else None,
                          model_profile_id=snapshot.get('model_profile_id'),
                          kb_id=snapshot.get('kb_id'), kb_ids=snapshot.get('kb_ids'),
                          capability_id=binding['capability_id'], capability_version=binding['capability_version'],
                          skill_refs=snapshot.get('skill_refs', []),
                          connector_tool_ids=[ref['id'] for ref in snapshot.get('connector_tools', [])],
                          memory=self._memory_selection(snapshot),
                          project_id=current.get('project_id'),
                          revises_run_id=run_id if in_place else None,
                          attachment_ids=attachment_ids)
            job = self.start(**kwargs)
            return {**job, 'regenerates_run_id':run_id, 'branched':not in_place}

    def delete_answer(self, thread_id, message_id):
        with self.lock:
            if thread_id in self.thread_runs:
                raise ValueError("请等待当前任务完成或先停止它。")
            binding = self.binding(thread_id)
            capability = self.registry.resolve(binding["capability_id"], binding["capability_version"])
            delete = getattr(capability, 'delete_answer', None)
            if delete is None:
                raise ValueError("当前伙伴不支持删除单条回答。")
            delete(thread_id, message_id)
            return self.inspect(thread_id)

    def run(self, jid, tid, capability, prepared, stop):
        with self.lock:
            if not stop.is_set():
                self.set_status(jid, "running")
        self.emit(jid, "started", thread_id=tid)
        result, error, status = None, None, "failed"
        try:
            result = capability.execute(prepared, stop, lambda kind, **data: self.emit(jid, kind, **data))
            result = normalize_result(result, thread_id=tid)
            status = result["status"]
            # 校验结果可持久化；失败仍进入统一结束流程。
            result = json.dumps(result, ensure_ascii=False)
        except RunStopped:
            status = "stopped"
        except Exception as exc:
            status, result = "failed", None
            error = "任务未完成。请检查模型配置、网络或目录权限，然后从会话中继续。"
            logger.exception("Job %s failed in thread %s", jid, tid)
            self.emit(jid, "problem", message=error, error_type=type(exc).__name__)
        finally:
            # 终态与释放活动任务必须和 cancel 使用同一把锁，避免已结束
            # 的任务在极短窗口内被取消请求改写成 stopping。
            with self.lock:
                with self.connect() as db:
                    db.execute("UPDATE jobs SET status=?, error=?, result=?, ended_at=? WHERE id=?", (status, error, result, now(), jid))
                self.traces.finish_job({"id":jid,"trace_id":prepared.snapshot['run_id'],"run_id":prepared.snapshot['run_id'],
                                        "thread_id":tid,"capability_id":capability.id,"capability_version":capability.version,
                                        "resumed":prepared.request['resume']},status)
                self.emit(jid, "done", status=status, thread_id=tid)
                self.active_runs.pop(jid, None)
                if self.thread_runs.get(tid) == jid:
                    self.thread_runs.pop(tid, None)
                lane = execution_lane(prepared.snapshot)
                self.running_by_lane[lane] -= 1
                if self.running_by_lane[lane] <= 0:
                    self.running_by_lane.pop(lane, None)
                self._refresh_compat_active()
                self._dispatch_locked()

    def close(self):
        with self.lock:
            stops = [entry['stop'] for entry in self.active_runs.values()]
        for stop in stops:
            stop.set()
        self.pool.shutdown(wait=True)


class ClosingConnection:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, kind, value, traceback):
        try:
            self.db.rollback() if kind else self.db.commit()
        finally:
            self.db.close()
