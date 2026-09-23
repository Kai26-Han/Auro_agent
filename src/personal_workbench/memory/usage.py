"""Provider-reported tokens only; never record prompts or model responses."""
import time
from contextvars import ContextVar
from functools import wraps
from langchain_core.callbacks import BaseCallbackHandler

kind = ContextVar('memory_operation',default='memory')


def tracked(name):
    def decorate(fn):
        @wraps(fn)
        def call(*args,**kwargs):
            token=kind.set(name)
            try:return fn(*args,**kwargs)
            finally:kind.reset(token)
        return call
    return decorate


class UsageMeter(BaseCallbackHandler):
    def __init__(self,store):self.store=store
    def record(self,tokens,unknown,failed):
        with self.store.connect() as db:
            db.execute('INSERT INTO lifecycle_usage(kind,calls,tokens,unknown,failed,created) VALUES (?,1,?,?,?,?)',(kind.get(),tokens,unknown,failed,time.time()))
    def on_llm_end(self,response,**kwargs):
        values=[getattr(g,'message',None) for group in response.generations for g in group]
        usage=[m.usage_metadata for m in values if getattr(m,'usage_metadata',None)]
        total=sum(u.get('total_tokens',0) for u in usage)
        self.record(total,int(not usage),0)
    def on_llm_error(self,error,**kwargs):self.record(0,1,1)
