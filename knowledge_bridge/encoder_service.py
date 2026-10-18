"""One on-demand encoder per workspace; the child exits to release CUDA and RAM."""
from __future__ import annotations
import argparse
import hmac
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def enabled():
    return os.environ.get('KNOWLEDGE_BRIDGE_PROCESS_ROLE') in {'console','watch'} and os.environ.get('JIEDU_ENCODER_CHILD')!='1'


def root():
    from .workspace import active_workspace
    return active_workspace().state/'encoder'


def descriptor(directory):
    try:return json.loads((Path(directory)/'service.json').read_text('utf-8'))
    except (OSError,ValueError):return {}


def call(directory,action,payload=None,timeout=2):
    item=descriptor(directory)
    if not item.get('port') or not item.get('token'):raise ConnectionError('向量服务未启动')
    request=urllib.request.Request(f"http://127.0.0.1:{int(item['port'])}/{action}",
        data=json.dumps(payload or {}).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+item['token']})
    # A local model request must never be sent through a configured HTTP proxy.
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail=json.loads(exc.read()).get('error','向量服务请求失败')
        if exc.code==409:raise TimeoutError(detail) from None
        raise RuntimeError(detail) from None


def status(directory=None):
    try:return call(directory or root(),'status',timeout=.5)
    except (OSError,ValueError,RuntimeError):return {'state':'available_unloaded','runtime_state':'unloaded','pid':None,'shared_service':True}


def ensure(directory=None):
    from filelock import FileLock
    from .workspace import PROGRAM_ROOT
    directory=Path(directory or root());directory.mkdir(parents=True,exist_ok=True)
    with FileLock(str(directory/'start.lock'),timeout=20):
        current=status(directory)
        if current.get('pid'):return
        log=directory/'worker.log'
        if log.exists() and log.stat().st_size>2*1024*1024:os.replace(log,log.with_suffix('.previous.log'))
        env=os.environ.copy();env['JIEDU_ENCODER_CHILD']='1'
        from .package_layout import python_executable
        python=python_executable(PROGRAM_ROOT,windowless=os.name=='nt')
        with log.open('ab') as output:
            process=subprocess.Popen([str(python),'-m','knowledge_bridge.encoder_service','--root',str(directory)],
                cwd=PROGRAM_ROOT,env=env,stdin=subprocess.DEVNULL,stdout=output,stderr=output,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if status(directory).get('pid')==process.pid:return
            if process.poll() is not None:raise RuntimeError('向量服务未能启动，请查看运行记录')
            time.sleep(.1)
        raise TimeoutError('向量服务启动超时')


def encode(texts,*,local_only=False,batch_size=None,lock_timeout=None):
    ensure()
    result=call(root(),'encode',{'texts':list(texts),'local_only':local_only,'batch_size':batch_size,'lock_timeout':lock_timeout},timeout=600)
    import numpy as np
    return np.asarray(result['vectors'],dtype='float32')


def warmup():
    def load():
        try:ensure();call(root(),'warmup',timeout=180)
        except Exception:pass  # Status/next explicit request reports the actual load failure.
    current=status()
    if current.get('state') in {'loaded','loading'}:return False
    threading.Thread(target=load,name='encoder-warmup-request',daemon=True).start();return True


def stop(directory=None):
    try:return call(directory or root(),'stop',timeout=3)
    except (OSError,ValueError,RuntimeError):return {'stopped':True,'already_stopped':True}


class Backend:
    def __init__(self):self.module=None
    def module_ready(self):
        if self.module is None:
            from . import core
            self.module=core
        return self.module
    def status(self):return self.module.local_model_status() if self.module else {'state':'available_unloaded','runtime_state':'unloaded'}
    def warmup(self):self.module_ready().model(local_only=True)
    def encode(self,payload):
        return self.module_ready().embed(payload['texts'],local_only=bool(payload.get('local_only')),
            batch_size=payload.get('batch_size'),lock_timeout=payload.get('lock_timeout')).tolist()


def serve(directory,backend=None,idle_seconds=120,ready=None):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    backend=backend or Backend();token=secrets.token_urlsafe(32);gate=threading.Lock()
    state={'last_used':time.monotonic(),'busy':False,'stopping':False}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,code,data):
            raw=json.dumps(data).encode();self.send_response(code);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)));self.end_headers()
            try:self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError):pass
        def do_POST(self):
            if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token):return self.reply(403,{'error':'未授权'})
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<=size<=8*1024*1024:return self.reply(413,{'error':'向量请求超过单批预算'})
                payload=json.loads(self.rfile.read(size) or b'{}')
                if self.path=='/status':
                    public=backend.status()
                    if state['busy'] and public.get('state')!='loaded':public.update(state='loading',runtime_state='loading')
                    return self.reply(200,{**public,'pid':os.getpid(),'shared_service':True,'busy':state['busy'],'idle_seconds':idle_seconds})
                if self.path=='/stop':
                    state['stopping']=True
                    if not state['busy']:threading.Thread(target=server.shutdown,daemon=True).start()
                    return self.reply(200,{'stopped':not state['busy'],'finishing_current_request':state['busy']})
                if self.path not in {'/encode','/warmup'}:return self.reply(404,{'error':'无此操作'})
                if state['stopping']:return self.reply(409,{'error':'向量服务正在释放，稍后重试'})
                if self.path=='/encode' and (not isinstance(payload.get('texts'),list) or any(not isinstance(t,str) for t in payload['texts'])):
                    return self.reply(400,{'error':'无效向量输入'})
                wait=payload.get('lock_timeout');wait=600 if wait is None else max(0,min(600,float(wait)))
                if not gate.acquire(timeout=wait):return self.reply(409,{'error':'共享向量模型正忙'})
                try:
                    if state['stopping']:return self.reply(409,{'error':'向量服务正在释放，稍后重试'})
                    state['busy']=True;state['last_used']=time.monotonic()
                    result={'vectors':backend.encode(payload)} if self.path=='/encode' else (backend.warmup() or {'loaded':True})
                    self.reply(200,result)
                finally:
                    state['last_used']=time.monotonic();state['busy']=False;gate.release()
                    if state['stopping']:threading.Thread(target=server.shutdown,daemon=True).start()
            except TimeoutError as exc:self.reply(409,{'error':str(exc)})
            except Exception as exc:self.reply(500,{'error':type(exc).__name__+': '+str(exc)[:250]})
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    item={'pid':os.getpid(),'port':server.server_port,'token':token};target=directory/'service.json';temporary=target.with_suffix('.tmp')
    temporary.write_text(json.dumps(item),'utf-8');os.replace(temporary,target)
    done=threading.Event()
    def expire():
        while not done.wait(.5):
            if not state['busy'] and time.monotonic()-state['last_used']>=idle_seconds:
                state['stopping']=True;server.shutdown();return
    threading.Thread(target=expire,name='encoder-idle-release',daemon=True).start()
    if ready:ready.set()
    try:server.serve_forever(poll_interval=.2)
    finally:
        done.set();server.server_close()
        if descriptor(directory).get('token')==token:target.unlink(missing_ok=True)


if __name__=='__main__':
    args=argparse.ArgumentParser();args.add_argument('--root',required=True)
    serve(Path(args.parse_args().root))
