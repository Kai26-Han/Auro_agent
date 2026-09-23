"""工作台助手的用户配置层。

工作台助手是固定的默认能力，不能像普通伙伴那样删除、停用或更换路由 ID。
这个存储只保存用户可编辑的覆盖层；底层系统指令、审批、记忆和上下文规则始终由程序管理。
"""
import json
import sqlite3
from contextlib import contextmanager
from uuid import uuid4

from personal_workbench.capabilities import DEFAULT_CAPABILITY, DEFAULT_VERSION
from personal_workbench.file_tools import TOOL_INFO
from personal_workbench.partner_store import PartnerInput


DEFAULT_ASSISTANT_INPUT = {
    'name': '工作台助手',
    'description': '理解资料、回答问题，整理并保存学习笔记。',
    'instructions': '根据用户的目标完成学习与工作任务；先理解需求和边界，再给出清晰、可执行的回答。',
    'skill_refs': [],
    'tool_ids': list(TOOL_INFO),
    'connector_tool_ids': [],
    'model_profile_id': None,
    'suggested_kb_ids': [],
    'examples': [],
}

SYSTEM_CONFIG = [
    {'name': '运行身份', 'value': DEFAULT_CAPABILITY, 'description': '固定的默认助手 ID，用于历史会话和任务恢复。'},
    {'name': '核心系统指令', 'value': '由工作台管理', 'description': '用户指令作为定制层追加，不覆盖工具、资料与安全边界。'},
    {'name': '操作确认', 'value': '始终启用', 'description': '文件覆盖和外部写操作继续遵循工作台审批规则。'},
    {'name': '记忆与上下文', 'value': '跟随工作台全局设置', 'description': '配置页不会改变记忆引擎、作用域或上下文压缩机制。'},
]


class WorkbenchAssistantInput(PartnerInput):
    """与普通助手共用可编辑字段，但使用独立的存储和生命周期。"""


def _normalise(data):
    value = dict(data)
    value['name'] = value['name'].strip()
    value['description'] = value['description'].strip()
    value['instructions'] = value['instructions'].strip()
    value['tool_ids'] = sorted(set(value['tool_ids']))
    value['connector_tool_ids'] = sorted(set(value['connector_tool_ids']))
    value['suggested_kb_ids'] = sorted(set(value['suggested_kb_ids']))
    value['examples'] = [text.strip() for text in value['examples'] if text.strip()]
    return value


class AssistantProfileStore:
    def __init__(self, settings):
        self.path = settings.data_dir / 'assistant-profile.sqlite'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS profile(id TEXT PRIMARY KEY, version TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS versions(version TEXT UNIQUE NOT NULL, definition TEXT NOT NULL,
                                                     created INTEGER PRIMARY KEY AUTOINCREMENT);
            ''')
            if db.execute('SELECT 1 FROM profile WHERE id=?', (DEFAULT_CAPABILITY,)).fetchone() is None:
                definition = _normalise(DEFAULT_ASSISTANT_INPUT)
                db.execute('INSERT OR IGNORE INTO versions(version,definition) VALUES (?,?)',
                           ('default-v1', json.dumps(definition, ensure_ascii=False)))
                db.execute('INSERT INTO profile(id,version) VALUES (?,?)',
                           (DEFAULT_CAPABILITY, 'default-v1'))
            else:
                # Built-in capabilities may grow. Keep the untouched default
                # profile aligned while preserving every user-created version.
                current = db.execute('SELECT version FROM profile WHERE id=?',
                                     (DEFAULT_CAPABILITY,)).fetchone()['version']
                if current == 'default-v1':
                    db.execute('UPDATE versions SET definition=? WHERE version=?',
                               (json.dumps(_normalise(DEFAULT_ASSISTANT_INPUT), ensure_ascii=False),
                                'default-v1'))

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _versions(self):
        with self.db() as db:
            current = db.execute('SELECT version FROM profile WHERE id=?', (DEFAULT_CAPABILITY,)).fetchone()['version']
            rows = list(db.execute('SELECT version,definition FROM versions ORDER BY created DESC'))
        versions = []
        total = len(rows)
        for index, row in enumerate(rows):
            data = json.loads(row['definition'])
            versions.append({
                **data,
                'id': DEFAULT_CAPABILITY,
                # Routing remains fixed. profile_version identifies this editable overlay.
                'version': DEFAULT_VERSION,
                'profile_version': row['version'],
                'version_number': total - index,
                'source': 'builtin',
                'enabled': True,
                'archived': False,
                'customized': _normalise(data) != _normalise(DEFAULT_ASSISTANT_INPUT),
            })
        return current, versions

    def get(self):
        current, versions = self._versions()
        active = next(item for item in versions if item['profile_version'] == current)
        return {**active, 'revisions': versions, 'system_config': SYSTEM_CONFIG}

    def save(self, body):
        data = _normalise(body.model_dump())
        if not data['name'] or not data['instructions']:
            raise ValueError('请输入助手名称和角色说明。')
        if set(data['tool_ids']) - set(TOOL_INFO):
            raise ValueError('所选工具不可用。')
        if any(len(text) > 1000 for text in data['examples']):
            raise ValueError('示例问题须为 1–1000 个字符。')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT v.definition FROM profile p JOIN versions v ON v.version=p.version
                                WHERE p.id=?''', (DEFAULT_CAPABILITY,)).fetchone()
            previous = _normalise(json.loads(row['definition']))
            if previous != data:
                version = uuid4().hex
                db.execute('INSERT INTO versions(version,definition) VALUES (?,?)',
                           (version, json.dumps(data, ensure_ascii=False)))
                db.execute('UPDATE profile SET version=? WHERE id=?', (version, DEFAULT_CAPABILITY))
        return self.get()

    def reset(self):
        return self.save(WorkbenchAssistantInput.model_validate(DEFAULT_ASSISTANT_INPUT))

    def restore(self, profile_version):
        return self.save(self.input_for(profile_version))

    def input_for(self, profile_version):
        with self.db() as db:
            row = db.execute('SELECT definition FROM versions WHERE version=?', (profile_version,)).fetchone()
        if row is None:
            raise ValueError('助手历史配置不存在。')
        return WorkbenchAssistantInput.model_validate(json.loads(row['definition']))
