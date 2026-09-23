"""Immutable web evidence snapshots, separate from local knowledge indexes."""
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone


class WebSources:
    def __init__(self, settings):
        self.path = settings.data_dir / 'web_sources.sqlite'

    def save(self, row):
        row = dict(row)
        row['retrieved_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        digest = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        cid = 'c_'+digest[:24]
        row.update(id=cid, chunk_id=cid, source_id=cid, version=digest, page=None, paragraph=1,
                   start_line=1, end_line=max(1,len(row['text'].splitlines())), archived=False, external=True)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO sources VALUES (?, ?)', (cid, json.dumps(row, ensure_ascii=False)))
        return row

    def source(self, cid):
        if not re.fullmatch(r'c_[a-f0-9]{24}',cid) or not self.path.exists():
            raise ValueError('找不到这条联网引用。')
        with sqlite3.connect(self.path) as db:
            row = db.execute('SELECT payload FROM sources WHERE id=?',(cid,)).fetchone()
        if not row:
            raise ValueError('找不到这条联网引用。')
        return json.loads(row[0])

    def evidence(self, rows):
        hits, sources = [], []
        for row in rows:
            saved = self.save(row)
            hits.append({**row, 'citation':f"[[{saved['id']}]]"})
            sources.append({'chunk_id':saved['id'], 'path':row['title'], 'url':row['url'], 'kind':row['kind'],
                            'start':1, 'end':saved['end_line'], 'sha256':saved['version'], 'external':True})
        return {'hits':hits, 'sources':sources, 'evidence_found':bool(hits)}
