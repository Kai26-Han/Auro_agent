"""LM1 typed records and governance. SQLite remains the single commit authority."""
import json
import sqlite3
import unicodedata
from uuid import uuid4

PROFILE_FIELDS = {
    'preferred_name': '称呼', 'language': '交流语言', 'timezone': '时区',
    'coding_experience': '代码经验', 'explanation_style': '讲解方式',
    'learning_direction': '长期学习方向',
}
PERSONAL = ('personal', 'personal')


def scope_of(value=None):
    value = value or {}
    return value.get('scope_kind', 'personal'), value.get('scope_id', 'personal')


class FoundationStore:
    @property
    def global_scope(self):
        from .workspace import managed
        return managed(self.settings)

    def migrate_foundation(self, db):
        # Additive migration: preserve IDs, original labels, sources and versions.
        db.execute('BEGIN IMMEDIATE')
        columns = {r['name'] for r in db.execute('PRAGMA table_info(memories)')}
        additions = {'memory_type': "TEXT NOT NULL DEFAULT 'fact'", 'profile_key': "TEXT NOT NULL DEFAULT ''",
                     'scope_kind': "TEXT NOT NULL DEFAULT 'personal'", 'scope_id': "TEXT NOT NULL DEFAULT 'personal'",
                     'status': "TEXT NOT NULL DEFAULT 'active'", 'conditions': "TEXT NOT NULL DEFAULT ''",
                     'topic_key': "TEXT NOT NULL DEFAULT ''", 'locked': 'INTEGER NOT NULL DEFAULT 0',
                     'source_kind': "TEXT NOT NULL DEFAULT 'legacy'"}
        for name, definition in additions.items():
            if name not in columns:
                db.execute(f'ALTER TABLE memories ADD COLUMN {name} {definition}')
        if 'locked' not in columns:
            db.execute('UPDATE memories SET locked=manual')
            db.execute("UPDATE memories SET status='deleted' WHERE deleted=1")
        for sql in (
            "CREATE TABLE IF NOT EXISTS memory_profile_fields (space_id TEXT NOT NULL,key TEXT NOT NULL,label TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',PRIMARY KEY(space_id,key))",
            "CREATE TABLE IF NOT EXISTS memory_profile_field_hidden (space_id TEXT NOT NULL,key TEXT NOT NULL,PRIMARY KEY(space_id,key))",
            "CREATE UNIQUE INDEX IF NOT EXISTS memory_profile_field ON memories(space_id,profile_key) WHERE memory_type='profile' AND deleted=0",
            "CREATE INDEX IF NOT EXISTS memory_scope ON memories(space_id,scope_kind,scope_id,memory_type,status)",
            "CREATE TABLE IF NOT EXISTS memory_scopes (id TEXT PRIMARY KEY,space_id TEXT NOT NULL,kind TEXT NOT NULL,name TEXT NOT NULL,UNIQUE(space_id,kind,name))",
            "CREATE TABLE IF NOT EXISTS memory_reviews (id TEXT PRIMARY KEY,space_id TEXT NOT NULL,target_id TEXT NOT NULL,target_version INTEGER NOT NULL,proposal TEXT,status TEXT NOT NULL,reason TEXT NOT NULL,source_thread TEXT,source_run TEXT,created TEXT NOT NULL,resolved TEXT)",
            "CREATE TABLE IF NOT EXISTS memory_sources (memory_id TEXT NOT NULL,version INTEGER NOT NULL,source_kind TEXT NOT NULL,source_thread TEXT,source_run TEXT,quote TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(memory_id,version))",
            "CREATE TABLE IF NOT EXISTS memory_epochs (space_id TEXT PRIMARY KEY,epoch INTEGER NOT NULL DEFAULT 0)",
            "CREATE TABLE IF NOT EXISTS memory_blocked_topics (space_id TEXT NOT NULL,profile_key TEXT NOT NULL,PRIMARY KEY(space_id,profile_key))",
            "CREATE TABLE IF NOT EXISTS memory_manifests (id TEXT PRIMARY KEY,space_id TEXT NOT NULL,thread_id TEXT NOT NULL,run_id TEXT NOT NULL,scope_kind TEXT NOT NULL,scope_id TEXT NOT NULL,items TEXT NOT NULL,enabled INTEGER NOT NULL,error TEXT,created TEXT NOT NULL)",
        ):
            db.execute(sql)

    @staticmethod
    def touch_epoch(db, sid):
        db.execute('INSERT INTO memory_epochs VALUES (?,1) ON CONFLICT(space_id) DO UPDATE SET epoch=epoch+1', (sid,))

    def scopes(self, sid):
        self.space(sid)
        with self.connect() as db:
            return [{'id': 'personal', 'space_id': sid, 'kind': 'personal', 'name': '个人通用'},
                    *[dict(r) for r in db.execute('SELECT * FROM memory_scopes WHERE space_id=? ORDER BY rowid', (sid,))]]

    def require_langmem(self, sid):
        if self.space(sid)['engine'] != 'langmem':
            raise ValueError('此功能仅适用于 LangMem 记忆空间。')

    def validate_scope(self, sid, scope=PERSONAL):
        if scope == PERSONAL:
            self.space(sid)
            return
        self.require_langmem(sid)
        if not any((s['kind'], s['id']) == scope for s in self.scopes(sid)):
            raise ValueError('记忆范围不存在或不属于当前空间。')

    def create_scope(self, sid, body):
        self.require_langmem(sid)
        name = body.name.strip()
        if not name:
            raise ValueError('请填写范围名称。')
        with self.connect() as db:
            if db.execute('SELECT count(*) FROM memory_scopes WHERE space_id=?', (sid,)).fetchone()[0] >= 100:
                raise ValueError('每个空间最多创建 100 个范围。')
            item = {'id': uuid4().hex, 'space_id': sid, 'kind': body.kind, 'name': name}
            try:
                db.execute('INSERT INTO memory_scopes VALUES (:id,:space_id,:kind,:name)', item)
            except sqlite3.IntegrityError:
                raise ValueError('同类范围名称已存在。') from None
        return item

    def profile_fields(self, sid):
        self.require_langmem(sid)
        with self.connect() as db:
            hidden={r[0] for r in db.execute('SELECT key FROM memory_profile_field_hidden WHERE space_id=?',(sid,))}
            custom=[dict(r) for r in db.execute('SELECT key,label,description FROM memory_profile_fields WHERE space_id=? ORDER BY rowid',(sid,))]
        return [{'key':k,'label':v,'description':'','custom':False} for k,v in PROFILE_FIELDS.items() if k not in hidden] + [{**r,'custom':True} for r in custom if r['key'] not in hidden]

    def create_profile_field(self, sid, body):
        self.require_langmem(sid)
        label=body.label.strip()
        if not label:raise ValueError('请填写维度名称。')
        normalize=lambda v:unicodedata.normalize('NFKC',v).strip().casefold()
        key='custom_'+uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            hidden={r[0] for r in db.execute('SELECT key FROM memory_profile_field_hidden WHERE space_id=?',(sid,))}
            labels=[r[0] for r in db.execute('SELECT label FROM memory_profile_fields WHERE space_id=?',(sid,))]
            active_labels=[label for key,label in PROFILE_FIELDS.items() if key not in hidden]+labels
            if normalize(label) in {normalize(v) for v in active_labels}:
                raise ValueError('这个档案维度已存在，请编辑已有维度。')
            if len(labels)>=24:raise ValueError('最多添加 24 个自定义档案维度。')
            db.execute('INSERT INTO memory_profile_fields VALUES (?,?,?,?)',(sid,key,label,body.description.strip()))
            record=self._insert(db,sid,body.content.strip(),'fact',manual=True,metadata={'memory_type':'profile','profile_key':key,'locked':True}) if body.content.strip() else None
            self.touch_epoch(db,sid)
        return {'key':key,'label':label,'description':body.description.strip(),'custom':True,'memory':record}

    def delete_profile_field(self, sid, key):
        self.require_langmem(sid)
        fields={field['key']:field for field in self.profile_fields(sid)}
        if key not in fields:raise ValueError('档案维度不存在或已删除。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if fields[key]['custom']:
                db.execute('DELETE FROM memory_profile_fields WHERE space_id=? AND key=?',(sid,key))
            else:
                db.execute('INSERT OR IGNORE INTO memory_profile_field_hidden VALUES (?,?)',(sid,key))
            db.execute('INSERT OR IGNORE INTO memory_blocked_topics VALUES (?,?)',(sid,key))
            self.touch_epoch(db,sid)
        return {'deleted':True,'key':key,'label':fields[key]['label']}

    def validate_metadata(self, sid, body):
        self.validate_scope(sid, (body.scope_kind, body.scope_id))
        if body.memory_type == 'profile':
            self.require_langmem(sid)
            if body.profile_key not in {f['key'] for f in self.profile_fields(sid)} or (body.scope_kind, body.scope_id) != PERSONAL:
                raise ValueError('档案字段或作用范围无效。个人档案只保存个人通用信息。')
        elif body.profile_key:
            raise ValueError('事实记忆不能指定档案字段。')
        if body.memory_type == 'fact' and body.topic_key in (PROFILE_FIELDS if self.space(sid)['engine'] != 'langmem' else {f['key'] for f in self.profile_fields(sid)}):
            raise ValueError('此主题由个人档案管理，请修改对应档案字段。')

    def profile(self, sid):
        self.require_langmem(sid)
        with self.connect() as db:
            rows = {r['profile_key']: dict(r) for r in db.execute("SELECT * FROM memories WHERE space_id=? AND memory_type='profile' AND deleted=0", (sid,))}
        return {'fields': [{**field, 'memory': self.public_retention(rows[field['key']]) if field['key'] in rows else None} for field in self.profile_fields(sid)]}

    def epoch(self, sid):
        with self.connect() as db:
            row = db.execute('SELECT epoch FROM memory_epochs WHERE space_id=?', (sid,)).fetchone()
            return row['epoch'] if row else 0

    def source(self, db, row):
        db.execute('INSERT OR REPLACE INTO memory_sources VALUES (?,?,?,?,?,?,?)',
                   (row['id'], row['version'], row['source_kind'], row['source_thread'], row['source_run'], row['source_quote'], row['updated']))

    def change_state(self, mid, body):
        from .store import now
        self.require_langmem(self.get(mid)['space_id'])
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            before = db.execute('SELECT * FROM memories WHERE id=? AND version=? AND deleted=0', (mid, body.version)).fetchone()
            if not before:
                raise ValueError('记忆已变化或已删除，请刷新后再编辑。')
            db.execute('UPDATE memories SET status=?,version=version+1,updated=? WHERE id=?', (body.status, now(), mid))
            row = dict(db.execute('SELECT * FROM memories WHERE id=?', (mid,)).fetchone())
            self.touch_epoch(db, row['space_id'])
            self._change(db, row, 'ARCHIVE' if body.status == 'archived' else 'RESTORE', before=dict(before))
            db.execute("UPDATE memory_reviews SET status='stale',resolved=? WHERE target_id=? AND status='pending'", (now(), mid))
            db.execute('DELETE FROM memory_vectors WHERE memory_id=?', (mid,))
        return row

    def add_review(self, db, row, candidate, reason, thread, run):
        from .store import now
        proposal = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
        # Repeated observations should not create an inbox full of identical proposals.
        if db.execute("SELECT 1 FROM memory_reviews WHERE target_id=? AND target_version=? AND proposal=? AND status IN ('pending','kept')", (row['id'], row['version'], proposal)).fetchone():
            return False
        db.execute('INSERT INTO memory_reviews VALUES (?,?,?,?,?,?,?,?,?,?,NULL)',
                   (uuid4().hex, row['space_id'], row['id'], row['version'], proposal, 'pending', reason, thread, run, now()))
        return True

    def reviews(self, sid, scope=PERSONAL, page=1):
        self.require_langmem(sid)
        self.validate_scope(sid, scope)
        with self.connect() as db:
            where = "r.space_id=? AND r.status='pending' AND m.deleted=0 AND m.scope_kind=? AND m.scope_id=?"
            args = (sid, *scope)
            if self.global_scope:
                where=where.replace(" AND m.scope_kind=? AND m.scope_id=?", "");args=(sid,)
            total = db.execute(f'SELECT count(*) FROM memory_reviews r JOIN memories m ON m.id=r.target_id WHERE {where}', args).fetchone()[0]
            rows = db.execute(f'SELECT r.* FROM memory_reviews r JOIN memories m ON m.id=r.target_id WHERE {where} ORDER BY r.created DESC,r.rowid DESC LIMIT 30 OFFSET ?', (*args, (page-1)*30)).fetchall()
            items = [{**dict(r), 'proposal': json.loads(r['proposal']), 'target': dict(db.execute('SELECT * FROM memories WHERE id=?', (r['target_id'],)).fetchone())} for r in rows]
        return {'items': items, 'total': total, 'page': page}

    def resolve_review(self, rid, body):
        from .store import now, fingerprint
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            review = db.execute("SELECT * FROM memory_reviews WHERE id=? AND status='pending'", (rid,)).fetchone()
            if not review:
                raise ValueError('建议已处理或不存在，请刷新。')
            before = db.execute("SELECT * FROM memories WHERE id=? AND deleted=0 AND status='active'", (review['target_id'],)).fetchone()
            if not before or before['version'] != body.target_version or before['version'] != review['target_version']:
                raise ValueError('原记忆已变化，请刷新后重新处理建议。')
            if body.action == 'replace':
                proposal = json.loads(review['proposal'])
                db.execute("UPDATE memories SET content=?,category=?,source_thread=?,source_run=?,source_quote=?,source_kind='user_confirmed',manual=1,locked=1,version=version+1,updated=?,fingerprint=? WHERE id=?",
                           (proposal['content'], proposal.get('category', before['category']), review['source_thread'], review['source_run'], proposal['source_quote'], now(), fingerprint(proposal['content']), before['id']))
                row = dict(db.execute('SELECT * FROM memories WHERE id=?', (before['id'],)).fetchone())
                self._change(db, row, 'REVIEW_ACCEPT', before=dict(before), thread=review['source_thread'], run=review['source_run'])
                db.execute('DELETE FROM memory_vectors WHERE memory_id=?', (before['id'],))
                db.execute("UPDATE memory_reviews SET status='stale',resolved=? WHERE target_id=? AND status='pending'", (now(), before['id']))
            self.touch_epoch(db, before['space_id'])
            db.execute('UPDATE memory_reviews SET status=?,resolved=? WHERE id=?', ('accepted' if body.action == 'replace' else 'kept', now(), rid))
        return {'resolved': True}

    def record_manifest(self, frozen, result, error=None):
        from .store import now
        if not frozen.get('_thread_id') or not frozen.get('_run_id'):
            return
        items = [{k: r.get(k) for k in ('id', 'version', 'kind', 'memory_type', 'profile_key', 'scope_kind', 'scope_id', 'included', 'reason', 'score')} for r in [*result['items'],*result.get('episodes',[]),*result.get('rules',[])]]
        with self.connect() as db:
            db.execute('INSERT INTO memory_manifests VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (uuid4().hex, frozen['space_id'], frozen['_thread_id'], frozen['_run_id'], *scope_of(frozen), json.dumps(items), int(result['enabled']), error, now()))

    def manifests(self, sid, page=1):
        self.space(sid)
        with self.connect() as db:
            total = db.execute('SELECT count(*) FROM memory_manifests WHERE space_id=?', (sid,)).fetchone()[0]
            rows = db.execute('SELECT * FROM memory_manifests WHERE space_id=? ORDER BY rowid DESC LIMIT 30 OFFSET ?', (sid, (page-1)*30)).fetchall()
        return {'items': [{**dict(r), 'items': json.loads(r['items'])} for r in rows], 'total': total, 'page': page}

    def apply_langmem(self, sid, run, thread, text, facts, existing, revision, *, profile_changes=(), profile_existing=(), scope=PERSONAL, epoch=None, commit_hook=None):
        from .langmem_engine import RememberedFact
        from .store import canonical_memory, now, fingerprint
        self.validate_scope(sid, scope)
        summary = dict(added=0, updated=0, unchanged=0, rejected=0, conflicts=0)
        known = {r['id']: r for r in [*existing, *profile_existing]}
        proposals = [*facts, *profile_changes]
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            from .lifecycle import blocked
            if blocked(db,sid,thread,run):return {'status':'skipped','count':0}
            cfg = db.execute('SELECT * FROM config WHERE id=1').fetchone()
            current_epoch = db.execute('SELECT epoch FROM memory_epochs WHERE space_id=?', (sid,)).fetchone()
            if cfg['revision'] != revision or not json.loads(cfg['value'])['learn_memories'] or (epoch is not None and epoch != (current_epoch['epoch'] if current_epoch else 0)):
                self._run(db, sid, run, 'skipped', 'config_changed', summary, thread)
                return {'status': 'skipped', 'count': 0}
            if db.execute('SELECT 1 FROM processed WHERE space_id=? AND run_id=?', (sid, run)).fetchone():
                return {'status': 'skipped', 'count': 0}
            changed = 0
            for proposal in proposals:
                event = proposal.get('event')
                if event == 'NONE':
                    summary['unchanged'] += 1
                    continue
                try:
                    fact = RememberedFact.model_validate(proposal)
                except ValueError:
                    summary['rejected'] += 1
                    continue
                if changed >= 11 or event not in (None, 'ADD', 'UPDATE') or not fact.content.strip() or fact.source_quote not in text:
                    summary['rejected'] += 1
                    continue
                changed += 1
                key = proposal.get('profile_key', '')
                kind = proposal.get('memory_type', 'fact')
                if kind not in ('fact', 'profile') or (kind == 'profile' and (key not in {f['key'] for f in self.profile_fields(sid)} or scope != PERSONAL)) or (kind == 'fact' and (key or fact.topic_key in {f['key'] for f in self.profile_fields(sid)})):
                    summary['rejected'] += 1
                    continue
                if kind == 'profile' and db.execute('SELECT 1 FROM memory_blocked_topics WHERE space_id=? AND profile_key=?', (sid, key)).fetchone():
                    summary['rejected'] += 1
                    continue
                digest = fingerprint(fact.content)
                if db.execute('SELECT 1 FROM blocked_hashes WHERE space_id=? AND hash=?', (sid, digest)).fetchone():
                    summary['rejected'] += 1
                    continue
                target = None
                mid = proposal.get('id')
                if mid in known:
                    target = db.execute("SELECT * FROM memories WHERE id=? AND space_id=? AND deleted=0 AND status='active' AND version=?", (mid, sid, known[mid]['version'])).fetchone()
                    if target is None or (not self.global_scope and (target['scope_kind'], target['scope_id']) != scope) or target['memory_type'] != kind or (kind == 'profile' and target['profile_key'] != key):
                        summary['rejected'] += 1
                        continue
                elif event == 'UPDATE':
                    summary['rejected'] += 1
                    continue
                if not target and kind == 'profile':
                    target = db.execute("SELECT * FROM memories WHERE space_id=? AND memory_type='profile' AND profile_key=? AND deleted=0", (sid, key)).fetchone()
                    # Field created or changed since the extraction snapshot: never overwrite.
                    if target:
                        summary['rejected'] += 1
                        continue
                if not target and kind == 'fact' and fact.topic_key:
                    matches = db.execute("SELECT * FROM memories WHERE space_id=? AND scope_kind=? AND scope_id=? AND memory_type='fact' AND topic_key=? AND conditions=? AND status='active' AND deleted=0", (sid, *scope, fact.topic_key, fact.conditions)).fetchall()
                    if len(matches) == 1:
                        target = matches[0]
                        if target['id'] not in known or target['version'] != known[target['id']]['version']:
                            summary['rejected'] += 1
                            continue
                    elif matches:
                        summary['rejected'] += 1
                        continue
                if target and canonical_memory(target['content']) == canonical_memory(fact.content) and target['conditions'] == fact.conditions:
                    summary['unchanged'] += 1
                    continue
                if target and target['conditions'] != fact.conditions:
                    # Scope/conditions are immutable for automatic UPDATE. A separate ADD is required.
                    summary['rejected'] += 1
                    continue
                if target:
                    if target['locked'] or not proposal.get('correction', False):
                        reason = 'locked' if target['locked'] else 'ambiguous'
                        summary['conflicts'] += int(self.add_review(db, target, {**fact.model_dump(), 'profile_key': key, 'memory_type': kind}, reason, thread, run))
                        continue
                    db.execute("UPDATE memories SET content=?,category=?,source_quote=?,source_thread=?,source_run=?,source_kind='user_message',version=version+1,updated=?,fingerprint=?,topic_key=? WHERE id=?",
                               (fact.content, fact.category, fact.source_quote, thread, run, now(), digest, fact.topic_key or target['topic_key'], target['id']))
                    row = dict(db.execute('SELECT * FROM memories WHERE id=?', (target['id'],)).fetchone())
                    self._change(db, row, 'UPDATE', before=dict(target), thread=thread, run=run)
                    db.execute('DELETE FROM memory_vectors WHERE memory_id=?', (row['id'],))
                    db.execute("UPDATE memory_reviews SET status='stale',resolved=? WHERE target_id=? AND status='pending'", (now(), row['id']))
                    summary['updated'] += 1
                    continue
                duplicate = db.execute("SELECT 1 FROM memories WHERE space_id=? AND scope_kind=? AND scope_id=? AND memory_type=? AND profile_key=? AND fingerprint=? AND conditions=? AND deleted=0", (sid, *scope, kind, key, digest, fact.conditions)).fetchone()
                if not duplicate and kind == 'fact':
                    duplicate = next((row for row in db.execute("SELECT content FROM memories WHERE space_id=? AND scope_kind=? AND scope_id=? AND memory_type='fact' AND category=? AND conditions=? AND deleted=0",(sid,*scope,fact.category,fact.conditions)) if canonical_memory(row['content'])==canonical_memory(fact.content)),None)
                if duplicate:
                    summary['unchanged'] += 1
                    continue
                self._insert(db, sid, fact.content, fact.category, thread=thread, run=run, quote=fact.source_quote,
                             metadata=dict(memory_type=kind, profile_key=key, scope_kind=scope[0], scope_id=scope[1], conditions=fact.conditions, topic_key=key or fact.topic_key, locked=0))
                summary['added'] += 1
            from time import time
            targets={r[0] for r in db.execute('SELECT id FROM memories WHERE space_id=? AND source_thread=? AND source_run=?',(sid,thread,run))}
            targets.update(r[0] for r in db.execute("SELECT target_id FROM memory_reviews WHERE space_id=? AND source_thread=? AND source_run=? AND status='pending'",(sid,thread,run)))
            for source in known:
                for target in targets:
                    if source!=target:db.execute('INSERT OR IGNORE INTO lifecycle_edges VALUES (?,?,?,?)',(source,target,'consolidation_input',time()))
            count = summary['added'] + summary['updated']
            db.execute('INSERT INTO processed VALUES (?,?,?,?,?)', (sid, run, 'completed', count, now()))
            reason = 'conflicts_pending' if summary['conflicts'] else 'changes_applied' if count else 'candidates_rejected' if summary['rejected'] else 'no_change' if summary['unchanged'] else 'no_facts'
            self._run(db, sid, run, 'completed', reason, summary, thread)
            if commit_hook:
                commit_hook(db, summary)
        return {'status': 'completed', 'count': count, 'summary': summary}
