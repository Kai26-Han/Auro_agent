"""Agent 生成的技能建议：先暂存，用户审批后才进入正式技能目录。"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

import yaml
from langchain_core.tools import tool

from personal_workbench.skill_package import parse_package
from personal_workbench.skill_security import normalize_source, scan_skill_files
from personal_workbench.skill_store import SkillStore


class SkillProposalStore:
    def __init__(self, settings):
        self.settings=settings
        self.path=settings.data_dir/'skill-proposals.sqlite'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS proposals(
                    id TEXT PRIMARY KEY, action TEXT NOT NULL, name TEXT NOT NULL,
                    target_id TEXT, content TEXT NOT NULL, metadata TEXT NOT NULL,
                    state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS proposal_state ON proposals(state,created DESC);
            ''')

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    @staticmethod
    def _document(name,description,instructions):
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',name or '') or len(name)>64:
            raise ValueError('技能 name 须使用小写字母、数字和短横线。')
        if not description.strip() or not instructions.strip(): raise ValueError('技能简介和说明不能为空。')
        header=yaml.safe_dump({'name':name,'description':description.strip(),'metadata':{'version':'1.0'}},allow_unicode=True,sort_keys=False).strip()
        return f'---\n{header}\n---\n{instructions.strip()}\n'

    def submit(self,name,description,instructions,target_id=''):
        content=self._document(name,description,instructions)
        store=SkillStore(self.settings)
        target=None
        if target_id:
            target=store.get(target_id)
            if target['name']!=name: raise ValueError('修改建议的技能 name 必须与目标技能一致。')
        meta,files=parse_package('SKILL.md',content.encode())
        source=normalize_source({'kind':'agent','label':'Agent 建议','identifier':'pending','trust_level':'agent-created'},'SKILL.md')
        meta.update(source=source,content_hash='sha256:'+meta['revision'],security=scan_skill_files(files,skill_name=name,source=source))
        proposal_id=uuid4().hex;now=time.time()
        with self.lock,self.db() as db:
            db.execute('INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?)',(
                proposal_id,'update' if target else 'create',name,target_id or None,content,
                json.dumps(meta,ensure_ascii=False),'pending',now,now))
        return self.get(proposal_id)

    @staticmethod
    def _public(row):
        value=dict(row);value['metadata']=json.loads(value['metadata'])
        return value

    def get(self,proposal_id):
        with self.db() as db: row=db.execute('SELECT * FROM proposals WHERE id=?',(proposal_id,)).fetchone()
        if not row: raise ValueError('技能建议不存在。')
        return self._public(row)

    def list(self,state='pending'):
        with self.db() as db: rows=db.execute('SELECT * FROM proposals WHERE state=? ORDER BY created DESC',(state,)).fetchall()
        return [self._public(row) for row in rows]

    def approve(self,proposal_id,allowed_tools,accept_risk=False):
        with self.lock:
            proposal=self.get(proposal_id)
            if proposal['state']!='pending': raise ValueError('此技能建议已经处理。')
            source={**proposal['metadata']['source'],'identifier':'proposal:'+proposal_id}
            store=SkillStore(self.settings)
            preview=store.preview('SKILL.md',proposal['content'].encode(),source)
            try:
                result=store.commit(preview['preview_id'],allowed_tools,proposal['name'],proposal.get('target_id'),
                                    preview.get('activation'),False,accept_risk)
            except Exception:
                store.discard_preview(preview['preview_id']);raise
            with self.db() as db: db.execute('UPDATE proposals SET state=?,updated=? WHERE id=?',('approved',time.time(),proposal_id))
            return result

    def reject(self,proposal_id):
        with self.lock,self.db() as db:
            row=db.execute('SELECT state FROM proposals WHERE id=?',(proposal_id,)).fetchone()
            if not row: raise ValueError('技能建议不存在。')
            if row['state']!='pending': raise ValueError('此技能建议已经处理。')
            db.execute('UPDATE proposals SET state=?,updated=? WHERE id=?',('rejected',time.time(),proposal_id))
        return self.get(proposal_id)


def build_propose_skill_tool(settings):
    proposals=SkillProposalStore(settings)

    @tool
    def propose_skill(name: str, description: str, instructions: str, target_id: str = '') -> dict:
        """仅在用户明确要求创建或改进技能时，提交一份待用户审批的技能建议；不会直接安装或修改技能。"""
        try:
            item=proposals.submit(name,description,instructions,target_id)
            return {'proposal_id':item['id'],'state':'pending','name':item['name'],
                    'message':'技能建议已提交到技能广场，等待用户审核；当前尚未安装或修改任何技能。'}
        except ValueError as exc:
            return {'error':str(exc)}
    return propose_skill
