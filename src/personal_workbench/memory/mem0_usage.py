"""Count native provider calls without storing requests or responses."""
import time
class Meter:
    def __init__(self,client,store,kind):
        self.wrapped=client;self.store=store;self.kind=kind;self.tokens=None
        config=getattr(client,'config',None)
        if kind=='native_llm' and config is not None and hasattr(config,'response_callback'):
            def callback(_,response,params):
                usage=getattr(response,'usage',None)
                self.tokens=getattr(usage,'total_tokens',None)
            config.response_callback=callback
    def __getattr__(self,name):return getattr(self.wrapped,name)
    def call(self,method,*args,**kwargs):
        started=time.time();self.tokens=None;failed=True
        try:result=getattr(self.wrapped,method)(*args,**kwargs);failed=False;return result
        finally:
            with self.store.connect() as db:db.execute('INSERT INTO native_usage VALUES (?,1,?,?,?,?,?)',(self.kind,self.tokens or 0,int(self.tokens is None),int(failed),time.time()-started,time.time()))
    def generate_response(self,**kwargs):return self.call('generate_response',**kwargs)
    def embed(self,*args,**kwargs):return self.call('embed',*args,**kwargs)
