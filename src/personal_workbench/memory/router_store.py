"""Compatibility API routing to physically separate, engine-owned stores."""
from .registry import Registry, PROFILES

SID_METHODS = set("profile_fields create_profile_field delete_profile_field list candidates active create processed runs finish apply scopes require_langmem validate_scope create_scope validate_metadata profile epoch reviews manifests apply_langmem retrieval_status".split())
MID_METHODS = set("get edit delete history change_state".split())
WRITES = set("create_profile_field delete_profile_field create edit delete change_state create_scope resolve_review apply apply_langmem finish retrieval_status record_manifest".split())


class MemoryStore:
    def __init__(self, settings):
        from .store import EngineStore
        self.settings = settings
        self.registry = Registry(settings)
        self.registry.initialize()
        self.stores = {engine: EngineStore(settings, path=self.registry.directory(engine) / "store.sqlite", engine=engine, seed=False) for engine in PROFILES}

    def owner(self, sid):
        for engine, store in self.stores.items():
            try:
                store.space(sid)
                return engine
            except ValueError:
                pass
        raise ValueError("记忆空间不存在。")

    def owned(self, sid):
        return self.stores[self.owner(sid)]

    def by_id(self, value, table="memories"):
        found = []
        for engine, store in self.stores.items():
            with store.connect() as db:
                if db.execute(f"SELECT 1 FROM {table} WHERE id=?", (value,)).fetchone():
                    found.append(engine)
        if len(found) != 1:
            raise ValueError("记忆不存在或 ID 不唯一，请从对应方案空间操作。")
        return found[0]

    def active_engine(self):
        return self.registry.engine(self.registry.state()["active_profile_id"])

    @property
    def path(self):
        return self.stores[self.active_engine()].path

    def connect(self):
        return self.stores[self.active_engine()].connect()

    def config(self, engine=None):
        return self.stores[engine or self.active_engine()].config()

    def save_config(self, config):
        with self.registry.guard():
            return self.stores[config.engine].save_config(config)

    def spaces(self):
        rows = [{**s, "memory_profile_id": PROFILES[e]} for e, store in self.stores.items() for s in store.spaces()]
        with self.stores['langmem'].connect() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='episodes'").fetchone():
                counts = dict(db.execute("SELECT space_id,count(*) FROM episodes WHERE status='active' GROUP BY space_id"))
                for row in rows:
                    if row['engine']=='langmem':row['count'] += counts.get(row['id'],0)
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collaboration_rules'").fetchone():
                counts=dict(db.execute("SELECT space_id,count(*) FROM collaboration_rules WHERE state='active' GROUP BY space_id"))
                for row in rows:
                    if row['engine']=='langmem':row['count'] += counts.get(row['id'],0)
        path=self.registry.directory('mem0')/'native'/'management.sqlite'
        if path.exists():
            import sqlite3,json
            with sqlite3.connect(f'file:{path}?mode=ro',uri=True) as db:
                published=db.execute("SELECT value FROM meta WHERE key='published'").fetchone()
                if published and json.loads(published[0]):
                    counts=dict(db.execute("SELECT space_id,count(*) FROM controls WHERE state='active' GROUP BY space_id"))
                    for row in rows:
                        if row['engine']=='mem0':row['count']=counts.get(row['id'],0)
        from .workspace import managed, INITIAL
        if managed(self.settings):
            with self.registry.connect() as db:
                removed={r['id']:r['state'] for r in db.execute('SELECT id,state FROM removed_memory_spaces')}
                current=dict(db.execute('SELECT engine,space_id FROM workspace_spaces'))
            rows=[{**r,'is_default':r['id']==INITIAL[r['engine']],'is_current':r['engine']==self.active_engine() and r['id']==current[r['engine']],'deleting':removed.get(r['id'])=='deleting'} for r in rows if removed.get(r['id'])!='deleted']
        return rows

    def space(self, sid):
        engine = self.owner(sid)
        return {**self.stores[engine].space(sid), "memory_profile_id": PROFILES[engine]}

    def create_space(self, name, engine=None):
        engine = engine or self.active_engine()
        with self.registry.guard():
            self.registry.require_active(PROFILES[engine])
            return self.stores[engine].create_space(name, engine)

    def __getattr__(self, name):
        if name not in SID_METHODS | MID_METHODS | {"resolve_review", "record_manifest"}:
            raise AttributeError(name)
        def call(first, *args, **kwargs):
            engine = (self.owner(first) if name in SID_METHODS else self.by_id(first) if name in MID_METHODS
                      else self.by_id(first, "memory_reviews") if name == "resolve_review" else self.owner(first["space_id"]))
            if name=='delete' and engine=='langmem':
                from . import MemoryService
                row=self.stores[engine].get(first)
                return MemoryService(self.settings).lifecycle.delete(row['space_id'],first)
            if engine=='mem0' and name in WRITES-{'retrieval_status','record_manifest','finish'}:
                from .mem0_native_store import NativeStore
                native=NativeStore(self.registry)
                if native.ready or native.meta('migration_started'):raise ValueError('Mem0 已启用原生管理，请使用原生记忆页面。')
            method = getattr(self.stores[engine], name)
            if name in WRITES:
                with self.registry.guard():
                    self.registry.require_active(PROFILES[engine])
                    return method(first, *args, **kwargs)
            return method(first, *args, **kwargs)
        return call
