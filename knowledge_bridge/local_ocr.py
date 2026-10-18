"""Managed local OCR: bounded pipe protocol, no document files in worker runtime."""
import ctypes
from ctypes import wintypes as w
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from filelock import FileLock, Timeout
from .storage import StorageCancelledError
from .package_layout import component

IMAGE_FORMATS = {'.png', '.jpg', '.jpeg', '.bmp', '.webp', '.tif', '.tiff'}
OCR_FORMATS = IMAGE_FORMATS | {'.pdf'}
MAX_SECONDS = 180
WARNING = 'OCR 自动识别，可能有误。数字、日期、金额请回原图核实；高分不等于正确，表格、公式、手写和阅读顺序不保证无损。'
MODEL_HASHES = {
    'PP-OCRv6_det_small.onnx': '090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f',
    'PP-OCRv6_rec_small.onnx': '6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884',
    'ch_ppocr_mobile_v2.0_cls_mobile.onnx': 'e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c',
}


def runtime_root():
    return Path(os.environ.get('KNOWLEDGE_BRIDGE_OCR_RUNTIME') or component('ocr') or Path(__file__).resolve().parents[1] / 'runtime' / 'ocr')


def runtime_python(root):
    standalone = root/'python'/'python.exe'
    return standalone if standalone.is_file() else root/'venv'/'Scripts'/'python.exe'


def runtime_status():
    root = runtime_root()
    ready = runtime_python(root).is_file() and all((root/'models'/name).is_file() for name in MODEL_HASHES)
    return {'ready': ready, 'engine': 'RapidOCR 3.9.2 / ONNX Runtime 1.29.0 CPU',
            'model': 'PP-OCRv6 small + mobile orientation', 'offline': True,
            'limits': {'input_mib': 16, 'pages': 20, 'pixels_per_page': 12_000_000,
                       'render_long_edge': 2400, 'seconds_per_document': 180, 'worker_memory_mib': 1024, 'concurrency': 1},
            'warning': WARNING, 'reason': None if ready else '本地 OCR 资源未准备完整；运行期不会下载，请修复界渡离线资源包'}


class WorkerJob:
    """Assign before sending plaintext. Closing job kills the exact child handles."""
    def __init__(self, pid):
        if os.name != 'nt': raise OSError('OCR resource isolation requires Windows')
        class Basic(ctypes.Structure):
            _fields_ = [('per_process',ctypes.c_int64),('per_job',ctypes.c_int64),('flags',w.DWORD),
                        ('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),('active',w.DWORD),
                        ('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class Io(ctypes.Structure):
            _fields_ = [(name,ctypes.c_uint64) for name in ('read','write','other','read_bytes','write_bytes','other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic',Basic),('io',Io),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),
                        ('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
        self.k = ctypes.WinDLL('kernel32',use_last_error=True)
        for name,args,result in [('CreateJobObjectW',[ctypes.c_void_p,w.LPCWSTR],w.HANDLE),
            ('SetInformationJobObject',[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD],w.BOOL),
            ('AssignProcessToJobObject',[w.HANDLE,w.HANDLE],w.BOOL),
            ('OpenProcess',[w.DWORD,w.BOOL,w.DWORD],w.HANDLE),('CloseHandle',[w.HANDLE],w.BOOL)]:
            fn=getattr(self.k,name);fn.argtypes=args;fn.restype=result
        self.handle=self.k.CreateJobObjectW(None,None)
        limit=Extended();limit.basic.flags=0x2000|0x200|0x8;limit.basic.active=2;limit.job_memory=1024*1024*1024
        process=self.k.OpenProcess(0x100|0x1,False,pid)
        try:
            if not self.handle or not process or not self.k.SetInformationJobObject(self.handle,9,ctypes.byref(limit),ctypes.sizeof(limit)) or not self.k.AssignProcessToJobObject(self.handle,process):
                raise ctypes.WinError(ctypes.get_last_error())
        except Exception:self.close();raise
        finally:
            if process:self.k.CloseHandle(process)
    def close(self):
        if self.handle:self.k.CloseHandle(self.handle);self.handle=None


def recognize_document(data, suffix, *, progress=None, cancel=None, preview_page=None):
    from .document_converter import ConversionResult
    engine='rapidocr-3.9.2-onnx-1.29.0-pdfium-5.13.0'
    def result(status,reason,markdown=None,metadata=None):
        return ConversionResult(status,suffix.lstrip('.'),engine,markdown,reason,
                                bool(metadata.get('automatic_ocr')) if metadata else True,metadata)
    if len(data)>16*1024*1024:return result('size_limit','OCR 输入超过 16 MiB；原件保留')
    root=runtime_root();status=runtime_status()
    if not status['ready'] and not ((suffix=='.pdf' or preview_page is not None) and runtime_python(root).is_file()):
        return result('ocr_unavailable',status['reason'])
    for name,digest in MODEL_HASHES.items():
        if preview_page is not None or (suffix=='.pdf' and not (root/'models'/name).is_file()):continue
        if hashlib.sha256((root/'models'/name).read_bytes()).hexdigest()!=digest:
            return result('ocr_unavailable','OCR 模型校验不通过；未执行识别')
    def check():
        if cancel and cancel():raise StorageCancelledError('OCR 已取消或保护会话失效')
    lock=FileLock(str(root/'worker.lock'));acquired=False;proc=None;job=None;threads=[]
    pages=[];total=None;failure=None;completed=False
    try:
        while not acquired:
            check()
            try:lock.acquire(timeout=0);acquired=True
            except Timeout:
                if progress:progress({'stage':'ocr_queued','message':'等待本机 OCR，当前串行处理'})
                time.sleep(.1)
        check();start=time.monotonic()
        env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','COMSPEC'}}
        env.update({'PYTHONDONTWRITEBYTECODE':'1','PYTHONUTF8':'1','OMP_NUM_THREADS':'2','OPENBLAS_NUM_THREADS':'2'})
        proc=subprocess.Popen([str(runtime_python(root)),'-I','-B',str(Path(__file__).with_name('ocr_worker.py'))],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,cwd=root,env=env,creationflags=subprocess.CREATE_NO_WINDOW)
        job=WorkerJob(proc.pid)
        events=queue.Queue()
        def pump():
            try:
                consumed=0
                while True:
                    line=proc.stdout.readline(2*1024*1024)
                    if not line:break
                    consumed+=len(line)
                    if consumed>8*1024*1024 or not line.endswith(b'\n'):raise ValueError('output_limit')
                    events.put(json.loads(line))
            except Exception:events.put({'kind':'error','reason':'worker_protocol_or_output_limit'})
            finally:events.put({'kind':'exit'})
        def feed():
            try:
                header=json.dumps({'size':len(data),'suffix':suffix,'models':str(root/'models'),'preview_page':preview_page}).encode()+b'\n'
                proc.stdin.write(header);proc.stdin.write(data);proc.stdin.close()
            except (OSError,ValueError):pass
        threads=[threading.Thread(target=pump,daemon=True),threading.Thread(target=feed,daemon=True)]
        for thread in threads:thread.start()
        while True:
            check()
            if time.monotonic()-start>MAX_SECONDS:failure='time_limit';break
            try:value=events.get(timeout=.1)
            except queue.Empty:continue
            kind=value.get('kind')
            if kind=='preview' and preview_page is not None:return value
            if kind=='stage':
                total=value.get('pages_total',total)
                if progress:progress({k:v for k,v in value.items() if k!='kind'} | {'message':f"本地 OCR：{value.get('page',0)}/{total or '?'} 页"})
            elif kind=='page':
                pages.append(value)
                if progress:progress({'stage':'ocr_page_done','pages_completed':len(pages),'pages_total':total,'message':f'已处理 {len(pages)}/{total} 页'})
            elif kind=='done':completed=True;total=value['pages_total'];break
            elif kind=='error':failure=value.get('reason','worker_failed');break
            elif kind=='exit':failure='worker_exit_or_memory_limit';break
        check()
    except StorageCancelledError:raise
    except Exception:failure='worker_start_or_isolation_failed'
    finally:
        if job:job.close()
        if proc:
            if proc.poll() is None:proc.kill()
            proc.wait(timeout=5)
            for thread in threads:thread.join(timeout=2)
            if proc.stdout:proc.stdout.close()
            if proc.stdin and not proc.stdin.closed:proc.stdin.close()
        if acquired:lock.release()
    valid=[p for p in pages if p.get('regions')]
    issues=not completed or any(p['status'] not in {'recognized','blank'} for p in pages) or len(pages)!=(total or 0)
    state='partial' if valid and issues else 'converted' if valid else 'empty' if completed and not issues else 'failed'
    used_ocr=any(p.get('ocr_used') for p in pages)
    warning=WARNING if used_ocr else ('PDF 可提取原文；未执行 OCR。仍应按原件核对阅读顺序及覆盖。' if suffix=='.pdf' and valid else '未得到可确认正文；未执行有效 OCR，请核对原件及失败原因。')
    summary={'automatic_ocr':used_ocr,'warning':warning,'pages_total':total,'pages_processed':len(pages),
        'coverage_complete':completed and not issues,'failure':failure,'engine':engine,
        'models':MODEL_HASHES,'pages':[{k:v for k,v in p.items() if k not in {'kind','regions'}} for p in pages]}
    body=['> '+warning,'','识别状态：'+state+'；处理页数：'+str(len(pages))+'/'+str(total or '未知'),
          '位置为原页左上角起算的归一化四边形；分数为引擎原始返回值，原文提取没有 OCR 分数。','']
    for p in pages:
        body += ['## 第 '+str(p['page'])+' 页 · '+p['status'],'']
        for i,r in enumerate(p.get('regions',[]),1):
            body += [f"### 区域 {i} · {r['method']}",
                     ('[待核对候选：'+r['review_reason']+'；不作为确认正文] ' if r.get('review_reason') else '')+r['text'],'',
                     '位置/引擎分数：`'+json.dumps({'quad':r['quad'],'score':r['score']},ensure_ascii=False)+'`','']
        if p.get('reason'):body.append('本页未完成：'+p['reason'])
    return result(state, '本地识别完成，仍需核对原件' if state=='converted' else '识别不完整或无正文；原件保留（'+str(failure or state)+'）',
                  '\n'.join(body) if valid else None,summary)
