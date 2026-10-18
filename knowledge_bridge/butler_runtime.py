"""One private, loopback-only llama.cpp child owned by the Console process."""
import ctypes
import hashlib
import json
import os
import subprocess
import threading
import time
import secrets
from pathlib import Path
import httpx


KNOWN_THINKING={'b252c5610a42ca82d20fe2a12813e9d069eed89292907e26c783eeb0bc961bc7':'switchable'} # Fixed 4B hash, verified switch behavior 2026-09-30.
def thinking_mode(config):return config.get('thinking_mode') or KNOWN_THINKING.get(config.get('model_sha256'),'unverified')

class LocalButlerRuntime:
    def __init__(self, context):
        self.context=context;self.root=context.state/'local-butler'
        self.config_path=self.root/'runtime.json';self.process=None;self._job=None
        self._lock=threading.RLock();self.error=None;self.ready=False;self._key=None

    def config(self):
        try:return json.loads(self.config_path.read_text('utf-8'))
        except FileNotFoundError:return {'enabled':False,'port':8877,'context_tokens':8192,'max_output_tokens':512}

    def status(self):
        config=self.config();code=self.process.poll() if self.process else None
        running=self.process is not None and code is None
        return {'enabled':bool(config.get('enabled')),'state':('ready' if self.ready else 'loading') if running else 'failed' if self.error or code is not None else 'stopped' if config.get('enabled') else 'not_configured',
                'pid':self.process.pid if running else None,'exit_code':code,'error':self.error,
                'model':config.get('model_name'),'host':'127.0.0.1','port':config.get('port',8877),
                'thinking_mode':thinking_mode(config),'max_output_tokens':config.get('max_output_tokens',8192),'context_tokens':config.get('context_tokens',8192),'gpu_layers':config.get('gpu_layers',16),'read_only':True}

    def endpoint(self):return 'http://127.0.0.1:'+str(int(self.config().get('port',8877)))

    def headers(self):
        if not self._key:raise RuntimeError('模型实例尚未启动')
        return {'Authorization':'Bearer '+self._key}

    def _component(self, value):
        path=(self.context.control/str(value)).resolve()
        if not path.is_relative_to(self.context.control.resolve()) or not path.is_file():
            raise ValueError('本地管家组件不存在或不在当前工作区组件目录')
        return path

    def _own_windows_child(self):
        if os.name!='nt':return
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_=[('per_process',ctypes.c_int64),('per_job',ctypes.c_int64),('flags',w.DWORD),
                      ('minimum',ctypes.c_size_t),('maximum',ctypes.c_size_t),('active',w.DWORD),
                      ('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class IO(ctypes.Structure):_fields_=[(name,ctypes.c_uint64) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Limits(ctypes.Structure):
            _fields_=[('basic',Basic),('io',IO),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR];kernel.CreateJobObjectW.restype=w.HANDLE
        kernel.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
        kernel.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
        kernel.CloseHandle.argtypes=[w.HANDLE]
        job=kernel.CreateJobObjectW(None,None);limits=Limits();limits.basic.flags=0x2000
        if not job or not kernel.SetInformationJobObject(job,9,ctypes.byref(limits),ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(job,int(self.process._handle)):
            if job:kernel.CloseHandle(job)
            raise OSError('无法将模型进程绑定到本次Console生命周期')
        self._job=(kernel,job)

    def start(self, cancel):
        with self._lock:
            config=self.config()
            if not config.get('enabled'):raise ValueError('本地管家模型尚未配置完成')
            if self.process is not None and self.process.poll() is None and self.ready:return
            self.close()
            binary=self._component(config['server_path']);model=self._component(config['model_path'])
            for path,expected in [(binary,config['server_sha256']),(model,config['model_sha256'])]:
                with path.open('rb') as handle:actual=hashlib.file_digest(handle,'sha256').hexdigest()
                if actual!=expected:raise ValueError('本地管家组件SHA256校验失败')
            for spec in config.get('model_files',[]):
                path=self._component(spec['path'])
                with path.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
                if actual!=spec['sha256']:raise ValueError('模型分片SHA256校验失败')
            if cancel.is_set():raise InterruptedError('已取消')
            self.root.mkdir(parents=True,exist_ok=True)
            self._key=secrets.token_urlsafe(48)
            args=[str(binary),'--model',str(model),'--host','127.0.0.1','--port',str(int(config.get('port',8877))),
                  '--ctx-size',str(int(config.get('context_tokens',8192))),'--parallel','1',
                  '--n-gpu-layers',str(int(config.get('gpu_layers',16))),'--threads',str(int(config.get('threads',4))),
                  '--alias','jiedu-local-butler','--no-webui','--poll','0','--poll-batch','0','--api-key',self._key]
            self.error=None
            with (self.root/'runtime.log').open('ab') as log:
                self.process=subprocess.Popen(args,cwd=binary.parent,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            try:self._own_windows_child()
            except Exception:
                self.process.terminate();self.process.wait(timeout=10);raise
        try:
            deadline=time.monotonic()+120
            with httpx.Client(trust_env=False,timeout=2) as client:
                while time.monotonic()<deadline:
                    if cancel.is_set():raise InterruptedError('已取消模型准备')
                    if self.process.poll() is not None:raise RuntimeError('本地模型进程退出，请查看运行状态')
                    try:
                        # /health may be public. Authenticate a model route before sending private data.
                        response=client.get(self.endpoint()+'/v1/models',headers=self.headers())
                        rejected=client.get(self.endpoint()+'/v1/models',headers={'Authorization':'Bearer invalid-'+secrets.token_hex(16)}) if response.status_code==200 else None
                        if response.status_code==200 and rejected.status_code==401 and self.process.poll() is None:
                            self.ready=True;return
                    except httpx.HTTPError:pass
                    cancel.wait(.25)
            raise TimeoutError('模型启动超时')
        except Exception as error:
            self.close();self.error=str(error);raise

    def close(self):
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
            if self._job:
                kernel,job=self._job;kernel.CloseHandle(job);self._job=None
            self.ready=False;self.process=None;self._key=None
