"""技能目录元数据与不可变包版本；归档不删除历史运行需要的资源。"""
import hashlib
import json
import re
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

from personal_workbench.file_tools import TOOL_INFO
from personal_workbench.skill_package import parse_package, safe_path, digest
from personal_workbench.skill_security import normalize_source, scan_skill_files


class SkillStore:
    def __init__(self, settings):
        self.root = settings.data_dir.resolve()/'skills'
        if self.root.is_symlink(): raise ValueError('技能目录不能是符号链接。')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.quarantine = self.root/'.quarantine'
        if self.quarantine.is_symlink(): raise ValueError('技能隔离区不能是符号链接。')
        self.quarantine.mkdir(parents=True, exist_ok=True, mode=0o700)
        ignore = self.quarantine/'.ignore'
        if not ignore.exists(): ignore.write_text('*\n', encoding='utf-8')
        self.path = settings.data_dir/'skills.sqlite'
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS skills(id TEXT PRIMARY KEY,name TEXT,display_name TEXT,revision TEXT,enabled INTEGER,archived INTEGER,allowed_tools TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS revisions(skill_id TEXT,revision TEXT,metadata TEXT,created REAL,PRIMARY KEY(skill_id,revision));
                CREATE TABLE IF NOT EXISTS previews(id TEXT PRIMARY KEY,metadata TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS skill_audit(id TEXT PRIMARY KEY,skill_id TEXT,revision TEXT,action TEXT,detail TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS skill_migrations(name TEXT PRIMARY KEY,applied REAL);
                CREATE INDEX IF NOT EXISTS skill_audit_lookup ON skill_audit(skill_id,created DESC);
            ''')
            columns={row[1] for row in db.execute('PRAGMA table_info(skills)')}
            for name,definition in (
                ('auto_trigger','INTEGER NOT NULL DEFAULT 1'),
                ('user_invocable','INTEGER NOT NULL DEFAULT 1'),
                ('internal_only','INTEGER NOT NULL DEFAULT 0'),
                ('scripts_enabled','INTEGER NOT NULL DEFAULT 0'),
            ):
                if name not in columns: db.execute(f'ALTER TABLE skills ADD COLUMN {name} {definition}')
            # Product policy changed from opt-in to opt-out auto triggering. Apply
            # it once to existing user-visible skills, then preserve later user
            # choices instead of forcing the switch back on at every startup.
            migration = 'auto_trigger_default_v1'
            if not db.execute('SELECT 1 FROM skill_migrations WHERE name=?',(migration,)).fetchone():
                db.execute('UPDATE skills SET auto_trigger=1 WHERE internal_only=0')
                db.execute('INSERT INTO skill_migrations VALUES (?,?)',(migration,time.time()))
        self.cleanup()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10); db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def cleanup(self):
        with self.lock, self.db() as db:
            for row in db.execute('SELECT id FROM previews WHERE created<?',(time.time()-3600,)).fetchall():
                shutil.rmtree(self._preview_path(row['id']), ignore_errors=True)
                shutil.rmtree(self.root/('_preview_'+row['id']), ignore_errors=True)
                db.execute('DELETE FROM previews WHERE id=?',(row['id'],))

    def _preview_path(self, token):
        if not re.fullmatch(r'[a-f0-9]{32}', token): raise ValueError('导入预览无效。')
        return self.quarantine/token

    @staticmethod
    def normalize_metadata(meta):
        defaults = {'auto':True,'user_invocable':True,'internal_only':False,'keywords':[],'priority':50}
        support = meta.get('support_level')
        if not meta.get('compatible', True):
            support = 'incompatible'
        elif support not in {'full', 'partial'}:
            support = 'partial' if meta.get('source') and (
                meta.get('compatibility') or meta.get('omitted_count', 0)
            ) else 'full'
        limitations = list(meta.get('limitations') or [])
        if support == 'partial' and meta.get('compatibility') and meta['compatibility'] not in limitations:
            limitations.append(meta['compatibility'])
        return {
            'conflicts_with': [], 'connector_dependencies': [], 'scripts': [],
            'optional_tools': [],
            'output_contract': 'none',
            'source': None, 'omitted_files': [], 'omitted_count': 0,
            'content_hash': '',
            'security': {'scanner_version':'', 'verdict':'safe', 'scanned_files':0,
                         'finding_count':0, 'findings':[], 'trust_level':'local'},
            **meta,
            'support_level': support, 'limitations': limitations,
            'activation': {**defaults, **meta.get('activation', {})},
        }

    def preview(self, filename, data, source=None):
        metadata, files = parse_package(filename, data)
        normalized_source = normalize_source(source, filename)
        omitted_count = (source or {}).get('omitted_count', 0)
        if omitted_count and metadata.get('compatible'):
            metadata['support_level'] = 'partial'
            metadata.setdefault('limitations', []).append(
                f'当前工作台不会导入或执行其中 {omitted_count} 个不受支持的文件。'
            )
        metadata.update(
            source=normalized_source,
            omitted_files=(source or {}).get('omitted_files', []),
            omitted_count=omitted_count,
            content_hash='sha256:'+metadata['revision'],
            security=scan_skill_files(files, skill_name=metadata['name'], source=normalized_source),
        )
        self.cleanup()
        token = uuid4().hex
        stage = self._preview_path(token)
        with self.lock:
            try:
                stage.mkdir(mode=0o700)
                for name, content in files.items():
                    path = stage/name; path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                with self.db() as db:
                    db.execute('INSERT INTO previews VALUES (?,?,?)',(token,json.dumps(metadata,ensure_ascii=False),time.time()))
            except Exception:
                shutil.rmtree(stage,ignore_errors=True); raise
        return {**metadata,'preview_id':token}

    def get(self, sid):
        with self.db() as db:
            row = db.execute('SELECT * FROM skills WHERE id=?',(sid,)).fetchone()
            if not row: raise ValueError('技能不存在。')
            item = dict(row)
            stored_display_name = item['display_name']
            meta = db.execute('SELECT metadata,created FROM revisions WHERE skill_id=? AND revision=?',(sid,item['revision'])).fetchone()
            item.update(self.normalize_metadata(json.loads(meta['metadata'])))
            # The catalog label belongs to the installation, not the immutable
            # package revision. Preserve built-in localization and user edits.
            item['display_name'] = stored_display_name
            item['installed_at'] = item['created']
            item['updated_at'] = meta['created']
            item['allowed_tools'] = json.loads(item['allowed_tools'])
            item['enabled'], item['archived'] = bool(item['enabled']), bool(item['archived'])
            for key in ('auto_trigger','user_invocable','internal_only','scripts_enabled'):
                item[key]=bool(item[key])
            manifest_file = next((file for file in item.get('files', [])
                                  if file.get('path') == 'manifest.json' and file.get('readable')), None)
            if manifest_file:
                path = self.root/sid/item['revision']/'manifest.json'
                try:
                    data = path.read_bytes()
                    valid = (len(data) == manifest_file['bytes']
                             and hashlib.sha256(data).hexdigest() == manifest_file['sha256'])
                    manifest = json.loads(data.decode('utf-8-sig')) if valid else {}
                except (OSError, UnicodeError, ValueError):
                    manifest = {}
                if isinstance(manifest, dict) and manifest.get('name') in (None, item['name']):
                    if not item.get('version') and isinstance(manifest.get('version'), str):
                        item['version'] = manifest['version'][:80]
                    localized = manifest.get('localized') or {}
                    zh = localized.get('zh') if isinstance(localized, dict) else None
                    display = (zh or {}).get('display_name') if isinstance(zh, dict) else None
                    display = display or manifest.get('display_name')
                    if item['display_name'] == item['name'] and isinstance(display, str) and display.strip():
                        item['display_name'] = display.strip()[:120]
            item['revisions'] = [self.normalize_metadata(json.loads(r['metadata'])) | {'created':r['created']} for r in db.execute('SELECT metadata,created FROM revisions WHERE skill_id=? ORDER BY created DESC',(sid,))]
            from personal_workbench.skill_readiness import skill_readiness
            item.update(skill_readiness(item))
            return item

    def list(self):
        with self.db() as db: ids = [r['id'] for r in db.execute('SELECT id FROM skills ORDER BY created DESC')]
        return [self.get(sid) for sid in ids]

    @staticmethod
    def version_diff(current, candidate):
        before={item['path']:item['sha256'] for item in current.get('files',[])}
        after={item['path']:item['sha256'] for item in candidate.get('files',[])}
        return {
            'added':sorted(after.keys()-before.keys()),
            'removed':sorted(before.keys()-after.keys()),
            'changed':sorted(path for path in before.keys()&after.keys() if before[path]!=after[path]),
            'unchanged_count':sum(before[path]==after[path] for path in before.keys()&after.keys()),
        }

    def discard_preview(self, token):
        with self.lock, self.db() as db:
            db.execute('DELETE FROM previews WHERE id=?',(token,))
            shutil.rmtree(self._preview_path(token),ignore_errors=True)

    def revision(self, sid, revision, verify=True):
        if not re.fullmatch(r'[a-f0-9]{32}',sid) or not re.fullmatch(r'[a-f0-9]{64}',revision):
            raise ValueError('技能版本标识无效。')
        with self.db() as db:
            row = db.execute('SELECT metadata FROM revisions WHERE skill_id=? AND revision=?',(sid,revision)).fetchone()
        if not row: raise ValueError('技能版本不存在，无法继续。')
        meta = self.normalize_metadata(json.loads(row['metadata']))
        if verify:
            for file in meta['files']:
                self.read_bytes(sid,revision,file)
        return meta

    def read_bytes(self,sid,revision,file):
        root = self.root/sid/revision
        path = root/safe_path(file['path'])
        if any(p.is_symlink() for p in [self.root,self.root/sid,root,path,*path.parents]):
            raise ValueError('技能资源不能是符号链接。')
        try:
            with path.open('rb') as stream: data = stream.read(file['bytes']+1)
        except OSError: raise ValueError('技能版本文件缺失，请恢复原始文件。') from None
        if len(data)!=file['bytes'] or hashlib.sha256(data).hexdigest()!=file['sha256']:
            raise ValueError('技能版本文件已改变，请恢复原始文件。')
        return data

    def resource(self,sid,revision,path):
        safe_path(path)
        meta = self.revision(sid,revision,verify=False)
        file = next((f for f in meta['files'] if f['path']==path),None)
        if not file or not file['readable']: raise ValueError('只能读取当前技能版本内的文本资源。')
        return self.read_bytes(sid,revision,file).decode('utf-8-sig'), file['sha256']

    def commit(self,token,allowed_tools,display_name='',target_id=None,activation=None,allow_scripts=False,accept_risk=False):
        if not re.fullmatch(r'[a-f0-9]{32}',token): raise ValueError('导入预览无效。')
        if set(allowed_tools)-set(TOOL_INFO): raise ValueError('所选工具不可用。')
        with self.lock, self.db() as db:
            row = db.execute('SELECT metadata,created FROM previews WHERE id=?',(token,)).fetchone()
            if not row or row['created']<time.time()-3600: raise ValueError('导入预览已过期，请重新选择文件。')
            meta = json.loads(row['metadata'])
            verdict = meta.get('security', {}).get('verdict', 'safe')
            if verdict == 'dangerous': raise ValueError('安全检查发现高风险内容，不能安装此技能。')
            if verdict == 'caution' and not accept_risk: raise ValueError('请先查看安全检查结果并确认风险。')
            activation={**meta.get('activation',{}),**(activation or {})}
            auto,user,internal=(bool(activation.get('auto')),bool(activation.get('user_invocable',True)),bool(activation.get('internal_only')))
            if internal and user: raise ValueError('内部技能不能同时允许用户手动选择。')
            if allow_scripts and not meta.get('scripts'): raise ValueError('此技能没有声明可执行脚本。')
            if allow_scripts and not meta.get('compatible'): raise ValueError('脚本包不兼容，不能授权执行。')
            stage = self._preview_path(token)
            # 确认前重新校验预览文件，避免预览后被替换。
            actual = {}
            for file in meta['files']:
                path=stage/file['path']
                if any(p.is_symlink() for p in [path,*path.parents]): raise ValueError('预览文件无效。')
                with path.open('rb') as stream: actual[file['path']]=stream.read(file['bytes']+1)
            if digest(actual)!=meta['revision']: raise ValueError('预览文件已改变，请重新导入。')
            if target_id:
                old=self.get(target_id)
                if old['name']!=meta['name']: raise ValueError('更新的技能 name 必须与原技能一致。')
                if any(r['revision']==meta['revision'] for r in old['revisions']): raise ValueError('此技能版本已存在。')
                sid=target_id
            else:
                if db.execute('SELECT id FROM skills WHERE name=?',(meta['name'],)).fetchone():
                    raise ValueError('同名技能已存在，请在详情中导入新版本或恢复归档技能。')
                sid=uuid4().hex
            destination=self.root/sid/meta['revision']
            if destination.parent.is_symlink(): raise ValueError('技能目录无效。')
            destination.parent.mkdir(exist_ok=True)
            try:
                shutil.copytree(stage,destination)
                db.execute('INSERT INTO revisions VALUES (?,?,?,?)',(sid,meta['revision'],json.dumps(meta,ensure_ascii=False),time.time()))
                if target_id:
                    db.execute('UPDATE skills SET revision=?,enabled=?,allowed_tools=?,auto_trigger=?,user_invocable=?,internal_only=?,scripts_enabled=? WHERE id=?',
                               (meta['revision'],int(meta['compatible'] and old['enabled']),json.dumps(allowed_tools),int(auto),int(user),int(internal),int(allow_scripts),sid))
                else:
                    db.execute('INSERT INTO skills(id,name,display_name,revision,enabled,archived,allowed_tools,created,auto_trigger,user_invocable,internal_only,scripts_enabled) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                               (sid,meta['name'],display_name.strip() or meta['name'],meta['revision'],int(meta['compatible']),0,json.dumps(allowed_tools),time.time(),int(auto),int(user),int(internal),int(allow_scripts)))
                audit_detail = {
                    'source': meta.get('source'), 'security': meta.get('security'),
                    'content_hash': meta.get('content_hash'), 'accepted_risk': bool(accept_risk),
                }
                db.execute('INSERT INTO skill_audit VALUES (?,?,?,?,?,?)',(
                    uuid4().hex,sid,meta['revision'],'update' if target_id else 'install',
                    json.dumps(audit_detail,ensure_ascii=False),time.time()))
                db.execute('DELETE FROM previews WHERE id=?',(token,))
                db.commit()
            except Exception:
                db.rollback(); shutil.rmtree(destination,ignore_errors=True); raise
            shutil.rmtree(stage,ignore_errors=True)
        return self.get(sid)

    def update(self,sid,changes):
        with self.lock:
            item=self.get(sid)
            if changes.get('enabled') and not item['compatible']: raise ValueError('此技能不兼容，无法启用。')
            if changes.get('allowed_tools') is not None and set(changes['allowed_tools'])-set(TOOL_INFO): raise ValueError('所选工具不可用。')
            if 'display_name' in changes and not changes['display_name'].strip(): raise ValueError('请输入技能显示名称。')
            if changes.get('scripts_enabled') and not item.get('scripts'): raise ValueError('此技能没有声明可执行脚本。')
            merged={k:changes.get(k,item[k]) for k in ('auto_trigger','user_invocable','internal_only')}
            if merged['internal_only'] and merged['user_invocable']: raise ValueError('内部技能不能同时允许用户手动选择。')
            fields=[]; values=[]
            for key in ('enabled','archived','display_name','allowed_tools','auto_trigger','user_invocable','internal_only','scripts_enabled'):
                if key in changes:
                    fields.append(key+'=?')
                    values.append(json.dumps(changes[key]) if key=='allowed_tools' else changes[key])
            if fields:
                with self.db() as db:
                    db.execute('UPDATE skills SET '+','.join(fields)+' WHERE id=?',(*values,sid))
                    db.execute('INSERT INTO skill_audit VALUES (?,?,?,?,?,?)',(
                        uuid4().hex,sid,item['revision'],'settings',json.dumps(changes,ensure_ascii=False),time.time()))
        return self.get(sid)

    def restore_revision(self, sid, revision):
        with self.lock:
            current=self.get(sid)
            meta=self.revision(sid,revision)
            if current['revision']==revision: return current
            with self.db() as db:
                db.execute('UPDATE skills SET revision=?,enabled=? WHERE id=?',(
                    revision,int(meta['compatible'] and current['enabled']),sid))
                db.execute('INSERT INTO skill_audit VALUES (?,?,?,?,?,?)',(
                    uuid4().hex,sid,revision,'restore',json.dumps({
                        'from_revision':current['revision'],'to_revision':revision,
                    },ensure_ascii=False),time.time()))
        return self.get(sid)

    def revision_enabled_state(self, sid, revision):
        """Return the last explicit enabled choice made for one installed revision."""
        for entry in self.audit(sid):
            if entry['revision'] != revision or entry['action'] != 'settings':
                continue
            if 'enabled' in entry['detail']:
                return bool(entry['detail']['enabled'])
        return None

    def audit(self, sid):
        self.get(sid)
        with self.db() as db:
            rows=db.execute('SELECT id,skill_id,revision,action,detail,created FROM skill_audit WHERE skill_id=? ORDER BY created DESC',(sid,)).fetchall()
        return [{**dict(row),'detail':json.loads(row['detail'])} for row in rows]
