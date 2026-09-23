"""Versioned public protocol shared by every capability implementation.

Capability internals may keep richer private state. Values are normalized at
the host boundary so persisted runs, SSE events and resumptions have one stable
contract and legacy v0 dictionaries remain readable.
"""
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PROTOCOL_VERSION = 1
SCHEMA_VERSION = 1
RunStatus = Literal[
    'created', 'queued', 'running', 'stopping', 'interrupted', 'stopped',
    'waiting_approval', 'completed', 'failed', 'limited', 'rejected',
    'conflict',
]
RUN_STATUSES = {
    'created', 'queued', 'running', 'stopping', 'interrupted', 'stopped',
    'waiting_approval', 'completed', 'failed', 'limited', 'rejected',
    'conflict',
}


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class StrictProtocolModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ExtensibleProtocolModel(BaseModel):
    # Capability-specific fields remain flat for backward-compatible clients.
    model_config = ConfigDict(extra='allow')


class RunRequest(StrictProtocolModel):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    request_schema_version: Literal[1] = SCHEMA_VERSION
    text: str | None = None
    thread_id: str = Field(min_length=1, max_length=64)
    existing: bool = False
    resume: bool = False
    decision: bool | Literal['session'] | None = None
    kb_id: str | None = None
    kb_ids: list[str] | None = None
    model_profile_id: str | None = None
    skill_refs: list[dict[str, Any]] = Field(default_factory=list)
    connector_tool_ids: list[str] = Field(default_factory=list)
    attachment_ids: list[str] = Field(default_factory=list, max_length=4)
    memory: dict[str, Any] | None = None
    project_id: str | None = Field(default=None, min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=128)
    revises_run_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode='after')
    def valid_transition(self):
        if self.resume and not self.existing:
            raise ValueError('恢复任务必须绑定已有会话。')
        if not self.resume and not (self.text or '').strip():
            raise ValueError('新任务必须包含用户请求。')
        return self


class PreparedRequest(RunRequest):
    """Validated public request plus capability-private execution fields."""
    model_config = ConfigDict(extra='allow')


class RunSnapshot(ExtensibleProtocolModel):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    snapshot_schema_version: Literal[1] = SCHEMA_VERSION
    capability_id: str = Field(min_length=1, max_length=128)
    capability_version: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)


class Usage(StrictProtocolModel):
    model_calls: int | None = Field(default=None, ge=0)
    tool_calls: int | None = Field(default=None, ge=0)
    usage_tokens: int | None = Field(default=None, ge=0)
    usage_unknown: bool = False


class ArtifactRef(ExtensibleProtocolModel):
    kind: Literal['file'] = 'file'
    name: str = Field(min_length=1, max_length=255)
    sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    bytes: int | None = Field(default=None, ge=0)
    quality: str | None = None


class RunResult(ExtensibleProtocolModel):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    result_schema_version: Literal[1] = SCHEMA_VERSION
    status: RunStatus
    thread_id: str = Field(min_length=1, max_length=64)
    output: str = ''
    usage: Usage | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)


class RunEvent(ExtensibleProtocolModel):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    event_schema_version: Literal[1] = SCHEMA_VERSION
    kind: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    emitted_at: str | None = None

    @model_validator(mode='after')
    def known_shape(self):
        required = {
            'started': ('thread_id',), 'progress': ('label',),
            'message': ('text',), 'stage': ('node_id', 'status'),
            'skill_loaded': ('name', 'revision'),
            'skill_resource': ('path',), 'problem': ('message',),
            'trace_span': ('span_id', 'span_type', 'status'),
            'done': ('status', 'thread_id'),
        }.get(self.kind, ())
        missing = [key for key in required if getattr(self, key, None) is None]
        if missing:
            raise ValueError('运行事件缺少字段：' + '、'.join(missing))
        if self.kind in {'stage', 'done'} and getattr(self, 'status', None) not in RUN_STATUSES | {'pending', 'skipped', 'superseded'}:
            raise ValueError('运行事件状态无效。')
        return self


class ApprovalRequest(ExtensibleProtocolModel):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    approval_schema_version: Literal[1] = SCHEMA_VERSION
    approval_id: str
    kind: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    message: str = ''


def _check_version(data, field='protocol_version'):
    version = data.get(field, 0)
    if not isinstance(version, int) or version < 0:
        raise ValueError('运行协议版本无效。')
    if version > PROTOCOL_VERSION:
        raise ValueError('运行数据来自更高协议版本，请升级工作台后重试。')


def normalize_request(data):
    value = dict(data)
    _check_version(value)
    value.setdefault('protocol_version', PROTOCOL_VERSION)
    value.setdefault('request_schema_version', SCHEMA_VERSION)
    return RunRequest.model_validate(value).model_dump()


def normalize_prepared_request(data, *, thread_id, run_id):
    value = dict(data)
    _check_version(value)
    value.setdefault('protocol_version', PROTOCOL_VERSION)
    value.setdefault('request_schema_version', SCHEMA_VERSION)
    prepared = PreparedRequest.model_validate(value).model_dump()
    if prepared['thread_id'] != thread_id or prepared['run_id'] != run_id:
        raise ValueError('能力不能替换任务或运行标识。')
    return prepared


def normalize_snapshot(data, capability_id, capability_version, run_id):
    value = dict(data or {})
    _check_version(value)
    if value.get('capability_id') not in (None, capability_id):
        raise ValueError('运行快照绑定了不同能力。')
    if value.get('capability_version') not in (None, capability_version):
        raise ValueError('运行快照绑定了不同能力版本。')
    value.update({
        'protocol_version': PROTOCOL_VERSION,
        'snapshot_schema_version': SCHEMA_VERSION,
        'capability_id': value.get('capability_id') or capability_id,
        'capability_version': value.get('capability_version') or capability_version,
        'run_id': value.get('run_id') or run_id,
    })
    # Capability snapshots may use an explicit null to distinguish "selected
    # nothing" from a field missing in an older schema. Preserve those values.
    return RunSnapshot.model_validate(value).model_dump()


def normalize_result(data, *, status=None, thread_id=None):
    value = dict(data or {})
    _check_version(value)
    value.update({
        'protocol_version': PROTOCOL_VERSION,
        'result_schema_version': SCHEMA_VERSION,
        'status': value.get('status') or status,
        'thread_id': value.get('thread_id') or thread_id,
    })
    return RunResult.model_validate(value).model_dump(exclude_none=True)


def normalize_event(data, *, emitted_at=None):
    value = dict(data)
    _check_version(value)
    value.update({
        'protocol_version': PROTOCOL_VERSION,
        'event_schema_version': SCHEMA_VERSION,
    })
    if emitted_at is not None:
        value.setdefault('emitted_at', emitted_at)
    return RunEvent.model_validate(value).model_dump(exclude_none=True)


def normalize_approval(data, approval_id):
    value = dict(data)
    _check_version(value)
    kind = value.get('kind') or ('overwrite' if value.get('filename') else 'approval')
    value.update({
        'protocol_version': PROTOCOL_VERSION,
        'approval_schema_version': SCHEMA_VERSION,
        'approval_id': approval_id,
        'kind': kind,
    })
    return ApprovalRequest.model_validate(value).model_dump(exclude_none=True)
