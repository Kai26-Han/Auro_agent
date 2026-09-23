"""LangMem retrieval with resumable full-space embedding generations.

A different embedding signature never serves a partial or older generation.
Completed vectors are durable; bounded builds can resume after failure/restart.
"""
import hashlib
import json
import time
from .vector_cache import VectorCache


class LangMemSearch:
    def __init__(self, settings, profile, embedding=None):
        from personal_workbench.rag_embedding import EndpointEmbedding
        self.settings,self.profile=settings,profile
        self.embedding=embedding or EndpointEmbedding(profile.embedding())

    def prepare(self,sid,query_vector=None,batch=64):
        from .store import MemoryStore
        from .lifecycle import unavailable
        owner=MemoryStore(self.settings); store=owner.owned(sid)
        owner.require_langmem(sid);owner.registry.require_active('langmem-default')
        vector=query_vector if query_vector is not None else self.embedding.get_query_embedding('索引维度检查')
        cache=VectorCache(self.settings,self.profile,len(vector))
        with store.connect() as db:
            rows=[dict(r) for r in db.execute("SELECT * FROM memories WHERE space_id=? AND memory_type='fact' AND status='active' AND deleted=0 ORDER BY id",(sid,)) if not unavailable(db,r['id'])]
            existing={r['memory_id']:r['fingerprint'] for r in db.execute('SELECT memory_id,fingerprint FROM memory_vectors WHERE space_id=? AND signature=?',(sid,cache.signature))}
        epoch=store.epoch(sid);state=owner.registry.state();revision=store.config()['revision']
        missing=[r for r in rows if existing.get(r['id'])!=hashlib.sha256(r['content'].encode()).hexdigest()]
        covered=len(rows)-len(missing);error=''
        try:
            for row in missing[:batch]:
                if owner.registry.state()!=state or store.epoch(sid)!=epoch or store.config()['revision']!=revision:raise ValueError('Index source changed')
                result=cache.vector(row,row['content'],self.embedding.get_text_embedding)
                if len(result)!=len(vector):raise ValueError('Embedding dimension changed')
                covered+=1
        except Exception:
            error='embedding_or_source_failed'
        with owner.registry.guard(),store.connect() as db:
            # Cache pages may survive, but a stale build cannot declare completion.
            current={(r['id'],r['version']) for r in db.execute("SELECT id,version FROM memories WHERE space_id=? AND memory_type='fact' AND status='active' AND deleted=0",(sid,)) if not unavailable(db,r['id'])}
            if owner.registry.state()!=state or store.epoch(sid)!=epoch or store.config()['revision']!=revision or current!={(r['id'],r['version']) for r in rows}:error='source_changed'
            status='failed' if error else 'ready' if covered==len(rows) else 'building'
            if owner.registry.state()['active_profile_id']=='langmem-default':
                db.execute('INSERT INTO index_generations VALUES (?,?,?,?,?,?,?) ON CONFLICT(space_id,signature) DO UPDATE SET state=excluded.state,total=excluded.total,covered=excluded.covered,updated=excluded.updated,error=excluded.error',(sid,cache.signature,status,len(rows),covered,time.time(),error))
        return {'signature':cache.signature,'state':status,'total':len(rows),'covered':covered,'error':error},cache

    def search(self,sid,records,query,limit,threshold):
        from langgraph.store.memory import InMemoryStore
        if not records:return []
        query_vector=self.embedding.get_query_embedding(query)
        status,cache=self.prepare(sid,query_vector)
        if status['state']!='ready':raise ValueError('记忆索引尚未完整，请到生命周期与诊断继续构建。')
        vectors={r['content']:cache.vector(r,r['content'],self.embedding.get_text_embedding) for r in records}
        def embed(texts):return [vectors[text] if text in vectors else query_vector for text in texts]
        store=InMemoryStore(index={'dims':len(query_vector),'embed':embed,'fields':['content']})
        for row in records:store.put(('memories',sid),row['id'],{'content':row['content']})
        return [{'id':hit.key,'score':float(hit.score)} for hit in store.search(('memories',sid),query=query,limit=limit) if hit.score is not None and hit.score>=threshold]
