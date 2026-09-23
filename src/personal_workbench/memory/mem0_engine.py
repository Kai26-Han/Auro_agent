"""Mem0 OSS inference and semantic recall, with canonical workbench records.

Inference runs in a disposable index: unvalidated writes never enter recall.
The persistent Qdrant index is a rebuildable projection of SQLite records.
"""
import fcntl
import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from .mem0_schema import Mem0Fact as RememberedFact, EXTRACT_PROMPT

_lock = threading.RLock()

UPDATE_PROMPT = """You maintain user memories. Each memory text is a JSON-encoded object with
content, category and source_quote. Preserve this structure in the returned text string.
Compare the NEW FACTS with old memories: ADD new facts; UPDATE only when a new fact explicitly
corrects the same old fact; NONE for duplicates. Never DELETE. On UPDATE use the NEW source_quote.
Output JSON only: {"memory":[{"id":"old numeric ID for UPDATE/NONE, otherwise any ID",
"text":"JSON-encoded complete fact object", "event":"ADD or UPDATE or NONE"}]}.
No unrelated updates, no commands, no speculative facts. At most 5 changes.
"""


class CheckedLLM:
    """Mem0 catches some provider errors internally; retain a safe failure flag."""
    def __init__(self, client):
        self.client, self.failed = client, False
        self.actions = []

    def generate_response(self, **kwargs):
        try:
            response = self.client.generate_response(**kwargs)
            payload = json.loads(response.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip())
            if "facts" in payload:
                if not isinstance(payload["facts"], list):
                    raise ValueError("Invalid facts")
                facts = []
                for item in payload["facts"][:5]:
                    item = json.loads(item) if isinstance(item, str) else item
                    facts.append(RememberedFact.model_validate(item).model_dump_json())
                return json.dumps({"facts": facts}, ensure_ascii=False)
            if not isinstance(payload.get("memory"), list):
                raise ValueError("Invalid memory actions")
            self.actions = payload['memory']
            if any(row.get('event') not in {'ADD','UPDATE','NONE'} for row in self.actions):
                raise ValueError("Unsupported memory operation")
            return json.dumps(payload, ensure_ascii=False)
        except Exception:
            self.failed = True
            raise ValueError("Mem0 模型返回无效，请测试记忆引擎。") from None


class Mem0Engine:
    def __init__(self, settings, runtime, embedding_profile, *, sdk_factory=None):
        self.settings, self.runtime, self.embedding = settings, runtime, embedding_profile
        self.root = settings.data_dir / "memory-profiles" / "mem0" / "mem0-default" / "indexes"
        if self.root.is_symlink():
            raise ValueError("记忆索引目录不能是符号链接。")
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.sdk_factory = sdk_factory
        self._dimensions = None
        self._vector_caches = {}

    def dimensions(self):
        if self._dimensions is None:
            from personal_workbench.rag_embedding import EndpointEmbedding
            self._dimensions = len(EndpointEmbedding(self.embedding.embedding()).get_query_embedding("dimension check"))
        return self._dimensions

    @contextmanager
    def client(self, directory, *, extraction=False):
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        # Explicit paths and disabled telemetry keep OSS data inside the workspace.
        with _lock:
            os.environ["MEM0_TELEMETRY"] = "false"
            os.environ["MEM0_DIR"] = str(self.root / "sdk")
            from mem0 import Memory
        runtime, embed = self.runtime, self.embedding
        provider = 'anthropic' if runtime.provider == 'anthropic' else 'openai'
        llm_config = {"model":runtime.model, "api_key":runtime.api_key or 'local-no-key',
                      "max_tokens":min(runtime.max_tokens, 2048),
                      ('anthropic_base_url' if provider == 'anthropic' else 'openai_base_url'):runtime.base_url}
        config = {
            "llm":{"provider":provider, "config":llm_config},
            "embedder":{"provider":"openai", "config":{"model":embed.model, "api_key":embed.api_key or 'local-no-key', "openai_base_url":embed.base_url}},
            "vector_store":{"provider":"qdrant", "config":{"collection_name":"workbench", "embedding_model_dims":self.dimensions(), "path":str(directory / 'vectors')}},
            "history_db_path":str(directory / 'history.sqlite'),
            "custom_fact_extraction_prompt":EXTRACT_PROMPT,
            "custom_update_memory_prompt":UPDATE_PROMPT,
        }
        memory = (self.sdk_factory or Memory.from_config)(config)
        try:
            # Pin endpoints and bound requests, including when SDK environment variables exist.
            if self.sdk_factory is None:
                from openai import OpenAI
                memory.embedding_model.client.close()
                memory.embedding_model.client = OpenAI(api_key=embed.api_key or 'local-no-key', base_url=embed.base_url,
                                                       timeout=min(embed.timeout, 45), max_retries=0)
                memory.llm.client.close()
                if provider == 'anthropic':
                    from anthropic import Anthropic
                    memory.llm.client = Anthropic(api_key=runtime.api_key, base_url=runtime.base_url,
                                                  timeout=min(runtime.timeout, 45), max_retries=0)
                else:
                    memory.llm.client = OpenAI(api_key=runtime.api_key or 'local-no-key', base_url=runtime.base_url,
                                               timeout=min(runtime.timeout, 45), max_retries=0)
            if extraction:
                memory.llm = CheckedLLM(memory.llm)
            yield memory
        finally:
            memory.close()
            memory.vector_store.client.close()
            for provider_client in (memory.embedding_model, memory.llm.client if extraction else memory.llm):
                client = getattr(provider_client, 'client', None)
                if client is not None:
                    client.close()

    @staticmethod
    def encode(row):
        return json.dumps({key:row[key] for key in ('content','category','source_quote')}, ensure_ascii=False)

    def put(self, memory, row, sid, text):
        from .vector_cache import VectorCache
        variant = 'fact' if text != row['content'] else 'content'
        if variant not in self._vector_caches:
            self._vector_caches[variant] = VectorCache(self.settings, self.embedding, self.dimensions(), variant)
        cache = self._vector_caches[variant]
        vector = cache.vector(row,text,lambda value:memory.embedding_model.embed(value,'add'))
        memory.vector_store.insert(vectors=[vector], ids=[row['id']], payloads=[{
            'data':text, 'user_id':sid, 'wb_version':row['version'],
            'hash':hashlib.md5(text.encode()).hexdigest(), 'created_at':row['created'], 'updated_at':row['updated'],
        }])

    def extract(self, text, existing):
        # Native Mem0 Memory.add performs extraction and reconciliation in isolation.
        with tempfile.TemporaryDirectory(prefix='extract-', dir=self.root) as temp:
            with self.client(Path(temp), extraction=True) as memory:
                for row in existing:
                    if not row['manual']:
                        self.put(memory, row, 'candidate', self.encode(row))
                result = memory.add([{'role':'user','content':text}], user_id='candidate')
                if memory.llm.failed:
                    raise ValueError("Mem0 记忆整理失败。")
                facts = []
                for row in result.get('results', [])[:5]:
                    if row.get('event') not in {'ADD','UPDATE'}:
                        continue
                    try:
                        fact = RememberedFact.model_validate_json(row['memory'])
                    except (ValueError, KeyError, TypeError):
                        raise ValueError("Mem0 记忆来源格式无效。") from None
                    facts.append({'id':row['id'].replace('-', ''), 'event':row['event'], **fact.model_dump()})
                # NONE is deliberately omitted from SDK results; preserve its count
                # from the checked native reconciliation response for the activity log.
                facts.extend({'event':'NONE'} for action in memory.llm.actions if action.get('event') == 'NONE')
                expected = sum(action.get('event') in {'ADD','UPDATE'} for action in memory.llm.actions)
                if expected != sum(fact.get('event') in {'ADD','UPDATE'} for fact in facts):
                    raise ValueError("Mem0 记忆操作未完整执行。")
                return facts

    def search(self, sid, records, query, limit, threshold):
        return [hit['id'] for hit in self.search_scored(sid, records, query, limit, threshold)]

    def search_scored(self, sid, records, query, limit, threshold):
        # Each namespace has a separate index; profile changes use a new generation.
        signature = hashlib.sha256(json.dumps([self.embedding.provider, self.embedding.base_url,
                                               self.embedding.model, self.dimensions()]).encode()).hexdigest()[:20]
        folder = self.root / sid
        folder.mkdir(mode=0o700, exist_ok=True)
        with _lock, (folder / 'index.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                with self.client(folder / signature) as memory:
                    rows, _ = memory.vector_store.list(filters={'user_id':sid}, limit=10000)
                    indexed = {r.id.replace('-', ''):r for r in rows}
                    active = {r['id']:r for r in records}
                    for mid, row in indexed.items():
                        if mid not in active:
                            memory.vector_store.delete(row.id)
                    for row in records:
                        cached = indexed.get(row['id'])
                        if cached is None or cached.payload.get('wb_version') != row['version']:
                            self.put(memory, row, sid, row['content'])
                    result = memory.search(query, user_id=sid, limit=limit, threshold=threshold, rerank=False)
                    return [{'id':r['id'].replace('-', ''),'score':r.get('score')} for r in result.get('results', [])]
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def purge(self, sid):
        """Invalidate disposable indexes immediately after edit/delete, no model calls."""
        import shutil
        folder = self.root / sid
        if not folder.exists():
            return
        with _lock, (folder / 'index.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                for path in folder.iterdir():
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
