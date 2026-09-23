"""Shared embedding cache mechanism, routed to each record owner."""
import hashlib
import json
import math
from .store import MemoryStore


class VectorCache:
    def __init__(self, settings, profile, dimension, variant="content"):
        self.store = MemoryStore(settings)
        self.dimension = dimension
        self.signature = hashlib.sha256(json.dumps([
            profile.provider, profile.base_url, profile.model, dimension, variant
        ]).encode()).hexdigest()

    def vector(self, row, text, embed):
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self.store.owned(row['space_id']).connect() as db:
            cached = db.execute('SELECT vector,fingerprint FROM memory_vectors WHERE space_id=? AND memory_id=? AND signature=?',
                                (row['space_id'], row['id'], self.signature)).fetchone()
        if cached and cached['fingerprint'] == digest:
            vector=json.loads(cached['vector'])
            if len(vector)==self.dimension and all(isinstance(x,(int,float)) and math.isfinite(x) for x in vector):return vector
        vector = embed(text)
        if len(vector)!=self.dimension or not all(isinstance(x,(int,float)) and math.isfinite(x) for x in vector):
            raise ValueError("嵌入向量维度或数值无效。")
        with self.store.registry.guard(), self.store.owned(row['space_id']).connect() as db:
            if self.store.space(row['space_id'])['memory_profile_id'] != self.store.registry.state()['active_profile_id']:
                return vector
            # An edit/delete during the model call must not repopulate a stale cache.
            live = db.execute('SELECT version FROM memories WHERE id=? AND space_id=? AND deleted=0', (row['id'],row['space_id'])).fetchone()
            if live and live['version'] == row['version']:
                db.execute('INSERT OR REPLACE INTO memory_vectors VALUES (?,?,?,?,?)',
                           (row['space_id'],row['id'],self.signature,digest,json.dumps(vector)))
        return vector
