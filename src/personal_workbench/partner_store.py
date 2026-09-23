"""S2：伙伴定义与不可变版本，独立于通用运行路由。"""
import json
import sqlite3
from contextlib import contextmanager
from uuid import uuid4

from pydantic import BaseModel, Field, ConfigDict
from personal_workbench.file_tools import TOOL_INFO
from personal_workbench.connector_limits import MAX_CONNECTOR_TOOLS_PER_TURN
from personal_workbench.skill_runtime import MAX_SKILLS_PER_ACTOR


class BoundSkill(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[a-f0-9]{32}$')
    revision: str = Field(pattern=r'^[a-f0-9]{64}$')


class PartnerInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default='', max_length=1000)
    instructions: str = Field(min_length=1, max_length=12000)
    skill_refs: list[BoundSkill] = Field(default_factory=list, max_length=MAX_SKILLS_PER_ACTOR)
    tool_ids: list[str] = Field(default_factory=lambda: list(TOOL_INFO), max_length=len(TOOL_INFO))
    connector_tool_ids: list[str] = Field(default_factory=list, max_length=MAX_CONNECTOR_TOOLS_PER_TURN)
    model_profile_id: str | None = Field(default=None, max_length=64)
    suggested_kb_ids: list[str] = Field(default_factory=list, max_length=50)
    examples: list[str] = Field(default_factory=list, max_length=6)


class PartnerStatus(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool | None = None
    archived: bool | None = None


class PartnerStore:
    def __init__(self, settings):
        self.path = settings.data_dir / 'partners.sqlite'
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS partners(id TEXT PRIMARY KEY, version TEXT, enabled INTEGER, archived INTEGER);
                CREATE TABLE IF NOT EXISTS versions(id TEXT, version TEXT, definition TEXT, created INTEGER PRIMARY KEY AUTOINCREMENT, UNIQUE(id,version));
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10); db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def get(self, pid):
        with self.db() as db:
            row = db.execute('SELECT * FROM partners WHERE id=?', (pid,)).fetchone()
            if row is None: raise ValueError('伙伴不存在。')
            versions = [json.loads(r['definition']) for r in db.execute('SELECT definition FROM versions WHERE id=? ORDER BY created DESC', (pid,))]
        # Display numbers are scoped to a partner. Keep immutable routing IDs
        # unchanged, including old definitions and persisted run snapshots.
        versions = [{**definition, 'version_number': len(versions)-index}
                    for index, definition in enumerate(versions)]
        return {**versions[0], 'enabled': bool(row['enabled']), 'archived': bool(row['archived']), 'revisions': versions}

    def list(self):
        with self.db() as db: ids = [r['id'] for r in db.execute('SELECT id FROM partners ORDER BY rowid DESC')]
        return [self.get(pid) for pid in ids]

    def save(self, body, pid=None):
        data = body.model_dump()
        if not data['name'].strip() or not data['instructions'].strip(): raise ValueError('请输入伙伴名称和角色说明。')
        if set(data['tool_ids']) - set(TOOL_INFO): raise ValueError('所选工具不可用。')
        if any(not text.strip() or len(text) > 1000 for text in data['examples']): raise ValueError('示例问题须为 1–1000 个字符。')
        if pid: self.get(pid)
        pid = pid or 'partner-' + uuid4().hex
        data = {**data, 'name': data['name'].strip(),
                'connector_tool_ids':sorted(set(data['connector_tool_ids'])), 'tool_ids': sorted(set(data['tool_ids'])), 'suggested_kb_ids': sorted(set(data['suggested_kb_ids']))}
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT definition FROM versions WHERE id=? ORDER BY created DESC LIMIT 1', (pid,)).fetchone()
            previous = json.loads(previous['definition']) if previous else None
            if previous:
                previous['connector_tool_ids'] = sorted(set(previous.get('connector_tool_ids',[])))
                previous['tool_ids'] = sorted(set(previous['tool_ids']))
                previous['suggested_kb_ids'] = sorted(set(previous['suggested_kb_ids']))
            if previous is None or any(previous.get(key) != value for key, value in data.items()):
                definition = {**data, 'id': pid, 'version': uuid4().hex, 'source': 'local'}
                db.execute('INSERT INTO versions(id,version,definition) VALUES (?,?,?)', (pid,definition['version'],json.dumps(definition,ensure_ascii=False)))
                db.execute('INSERT INTO partners VALUES (?,?,1,0) ON CONFLICT(id) DO UPDATE SET version=excluded.version', (pid,definition['version']))
        return self.get(pid)

    def status(self, pid, changes):
        self.get(pid)
        with self.db() as db:
            for field in ('enabled', 'archived'):
                if field in changes: db.execute(f'UPDATE partners SET {field}=? WHERE id=?', (int(changes[field]),pid))
        return self.get(pid)

    def references(self, skill_id):
        return [{'id': p['id'], 'name': p['name'], 'archived': p['archived'], 'version': v['version'], 'version_number':v['version_number']}
                for p in self.list() for v in p['revisions'] if any(s['id']==skill_id for s in v['skill_refs'])]
