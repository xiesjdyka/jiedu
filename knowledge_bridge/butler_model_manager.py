"""Owner-only model files: explicit downloads, pinned hashes, resumable partials."""
import hashlib,json,os,re,shutil,threading,time,uuid,ipaddress,socket,ssl,logging
from urllib.request import getproxies
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit,urljoin,urlunsplit
import httpx
from filelock import FileLock

class _DownloadLogFilter(logging.Filter):
    def filter(self,record):return record.threadName!='butler-model-download'
logging.getLogger('httpx').addFilter(_DownloadLogFilter())
logging.getLogger('httpcore').addFilter(_DownloadLogFilter())

def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);temporary=path.with_name('.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('w',encoding='utf-8') as stream:json.dump(value,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
        for attempt in range(4):
            try:os.replace(temporary,path);break
            except PermissionError:
                if attempt==3:raise
                time.sleep(.025*(attempt+1))
    finally:temporary.unlink(missing_ok=True)

def digest(path,cancel):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        while chunk:=stream.read(1024*1024):
            if cancel.is_set():raise InterruptedError('已暂停，已下载部分保留')
            value.update(chunk)
    return value.hexdigest()

class DownloadHTTPError(Exception):
    def __init__(self,status):self.status=status

def download_error(error):
    if isinstance(error,DownloadHTTPError):return '发布者返回HTTP '+str(error.status)
    if isinstance(error,httpx.TimeoutException):return '下载连接超时（'+type(error).__name__+'）'
    if isinstance(error,httpx.ConnectError):return '无法连接发布者或系统代理（ConnectError）'
    if isinstance(error,httpx.RemoteProtocolError):return '发布者提前中断传输（RemoteProtocolError）'
    if isinstance(error,httpx.TransportError):return '网络传输中断（'+type(error).__name__+'）'
    if isinstance(error,OSError):return '本机文件或网络访问失败（'+type(error).__name__+'，错误码'+str(getattr(error,'winerror',None) or getattr(error,'errno',None) or '未知')+'）'
    return '下载未完成（'+type(error).__name__+'），请核对网络及文件'

class ModelManager:
    def __init__(self,context,runtime,*,catalog=None,isolated=False):
        self.context=context;self.runtime=runtime;self.root=context.state/'local-butler/models';self.files=context.control/'models/butler-managed'
        self.catalog=catalog or json.loads(Path(__file__).with_name('butler_models.json').read_text('utf-8'))['models']
        self.lock=threading.RLock();self.worker=None;self.response=None;self.cancel_event=threading.Event();self.isolated=isolated;self.on_change=[]
    def _save(self,path,value):
        with self.lock:
            if path==self.root/'download.json':
                old=self._load('download.json',{})
                if old.get('run_id') and value.get('run_id')!=old['run_id']:return
                if old.get('state')=='cancelled' and value.get('run_id')==old.get('run_id'):
                    value.update(state='cancelled',message='已取消，已下载部分保留；恢复需再次主动选择')
            save(path,value)
    def _load(self,name,default):
        with self.lock:
            path=self.root/name
            return json.loads(path.read_text('utf-8')) if path.is_file() else default
    def status(self):
        with self.lock:
            installed=self._load('installed.json',[]);config=self.runtime.config()
            if config.get('model_path') and not any(r['id']==config.get('model_id','existing') for r in installed):
                installed.append({'id':config.get('model_id','existing'),'name':config.get('model_name','已有本机模型'),'config':config,'origin':'existing','thinking_mode':config.get('thinking_mode','unverified')})
            public=[{k:v for k,v in row.items() if k!='config'} for row in installed]
            job=self._load('download.json',None)
            if job and job['state'] in {'downloading','verifying','retrying'} and not (self.worker and self.worker.is_alive()):job={**job,'state':'paused','message':'上次下载已中断，可继续'}
            return {'catalog':self.catalog,'installed':public,'active':config.get('model_id','existing') if config.get('enabled') else None,
                'runtime_installed':bool(config.get('server_path')),'download':job,'download_directory':str(self.files),'runtime':self.runtime.status()}
    def _file(self,model,name):
        if not re.fullmatch('[a-zA-Z0-9_-]{1,80}',model['id']) or Path(name).name!=name or '/' in name or '\\' in name:raise ValueError('模型文件名无效')
        folder=self.files/model['id']/model['revision'];folder.mkdir(parents=True,exist_ok=True)
        if folder.resolve().is_relative_to(self.files.resolve()) is False:raise ValueError('模型文件越界')
        return folder/name
    def _url(self,url):
        parsed=urlsplit(url);host=parsed.hostname or ''
        if self.isolated and parsed.scheme=='http' and host=='127.0.0.1':return
        allowed=host=='huggingface.co' or host.endswith('.hf.co') or host.endswith('.xethub.hf.co')
        if parsed.scheme!='https' or not allowed or parsed.username or parsed.password:raise ValueError('模型下载重定向不在已知发布域名')
        if parsed.port not in {None,443} or parsed.fragment or len(url)>16384 or any(ord(c)<32 for c in url):raise ValueError('模型下载地址无效')
        # Signed publisher/CDN query parameters are permitted; no workspace data or credentials added.
    def start(self,model_id,network_mode='system_proxy',restart_failed=False):
        if network_mode not in {'direct','system_proxy'}:raise ValueError('请选择直连或系统代理')
        with self.lock:
            if self.worker and self.worker.is_alive():raise ValueError('已有下载进行中，请先暂停')
            model=next((r for r in self.catalog if r['id']==model_id),None)
            if model is None:raise ValueError('未知模型；只下载明确固定版本的清单')
            self.files.mkdir(parents=True,exist_ok=True)
            if restart_failed:
                previous=self._load('download.json',{})
                if previous.get('model_id')!=model_id or previous.get('state')!='failed':raise ValueError('仅可重新下载该失败任务的文件')
                spec=next((f for f in model['files'] if f['name']==previous.get('file')),None)
                if spec:
                    part=self._file(model,spec['name']).with_suffix('.gguf.part')
                    if part.is_file():part.rename(part.with_name(part.name+'.failed-'+uuid.uuid4().hex))
            needed=sum(max(0,f['bytes']-self._file(model,f['name']).with_suffix('.gguf.part').stat().st_size) if self._file(model,f['name']).with_suffix('.gguf.part').is_file() else f['bytes'] for f in model['files'])
            if shutil.disk_usage(self.files).free<needed+64*1024*1024:raise ValueError('可用磁盘空间不足；不会开始下载')
            self.cancel_event=threading.Event();run_id=uuid.uuid4().hex
            save(self.root/'download.json',{'run_id':run_id,'model_id':model['id'],'name':model['name'],'state':'downloading','received':model['bytes']-needed,'total':model['bytes'],'revision':model['revision'],'message':'正在建立下载连接','network_mode':network_mode})
            self.worker=threading.Thread(target=self._run,args=(model,network_mode,run_id),daemon=True,name='butler-model-download');self.worker.start()
            return {'state':'starting','model_id':model_id}
    def pause(self):
        self.cancel_event.set()
        response=self.response
        if response:
            try:response.close()
            except Exception:pass
        return {'state':'pausing','message':'已请求暂停，部分文件保留'}
    def cancel(self):
        with self.lock:
            job=self._load('download.json',None)
            if not job:raise ValueError('当前没有下载或导入任务')
            if job['state']=='completed':raise ValueError('该任务已完成，取消不会删除已安装模型')
            self.cancel_event.set();response=self.response
            job.update(state='cancelled',message='已取消，已下载部分保留；恢复需再次主动选择')
            self._save(self.root/'download.json',job)
        if response:
            try:response.close()
            except Exception:pass
        return {'state':'cancelled','message':job['message'],'partial_preserved':True}

    def _run(self,model,network_mode,run_id=None):
        job={'run_id':run_id,'model_id':model['id'],'name':model['name'],'state':'downloading','received':0,'total':model['bytes'],'revision':model['revision'],'message':'正在下载','network_mode':network_mode}
        job['received']=self._load('download.json',{}).get('received',0)
        last=0
        def record(force=False):
            nonlocal last
            if force or time.monotonic()-last>.2:self._save(self.root/'download.json',job);last=time.monotonic()
        self.root.mkdir(parents=True,exist_ok=True)
        try:
            proxy=getproxies().get('https') if network_mode=='system_proxy' else None
            with FileLock(str(self.root/'download.lock'),timeout=0),httpx.Client(proxy=proxy,verify=ssl.create_default_context(),trust_env=False,timeout=httpx.Timeout(30,connect=5),follow_redirects=False) as client:
                record(True);completed=0
                for spec in model['files']:
                    target=self._file(model,spec['name']);part=target.with_suffix('.gguf.part');job['file']=spec['name']
                    if target.is_file():
                        job['state']='verifying';record(True)
                        if target.stat().st_size!=spec['bytes'] or digest(target,self.cancel_event)!=spec['sha256']:raise ValueError('已下载模型完整性变化，请保留文件并核对')
                        completed+=spec['bytes'];continue
                    offset=part.stat().st_size if part.is_file() else 0
                    if offset>spec['bytes']:raise ValueError('部分文件长度异常，拒绝续传')
                    if offset<spec['bytes']:
                        for attempt in range(3):
                            offset=part.stat().st_size if part.is_file() else 0
                            job.update(state='downloading',received=completed+offset,retry=attempt,message='正在续传' if offset else '正在下载');record(True)
                            try:
                                url=spec['url']
                                for hop in range(6):
                                    if self.cancel_event.is_set():raise InterruptedError('已暂停，已下载部分保留')
                                    self._url(url)
                                    parsed=urlsplit(url);headers={'Range':'bytes='+str(offset)+'-'} if offset else {};extensions={};send_url=url
                                    if not self.isolated and not proxy:
                                        addresses=[ipaddress.ip_address(r[4][0]) for r in socket.getaddrinfo(parsed.hostname,443,type=socket.SOCK_STREAM)]
                                        if not addresses or any(not a.is_global for a in addresses):raise ValueError('下载地址指向私有网络')
                                        address=str(next((a for a in addresses if a.version==4),addresses[0]));authority='['+address+']' if ':' in address else address
                                        headers['Host']=parsed.netloc;extensions['sni_hostname']=parsed.hostname
                                        send_url=urlunsplit((parsed.scheme,authority,parsed.path,parsed.query,''))
                                    request=client.build_request('GET',send_url,headers=headers,extensions=extensions)
                                    if self.cancel_event.is_set():raise InterruptedError('下载已停止，部分文件保留')
                                    response=client.send(request,stream=True)
                                    if response.status_code in {301,302,303,307,308}:
                                        next_url=urljoin(url,response.headers.get('location',''));response.close();url=next_url;continue
                                    break
                                else:raise ValueError('模型下载重定向过多')
                                self.response=response
                                with closing(response):
                                    if response.status_code not in {200,206}:raise DownloadHTTPError(response.status_code)
                                    if response.status_code==200 and offset:raise ValueError('服务未接受断点续传，已下载部分保留；请稍后继续或明确选择重新下载')
                                    elif offset and (response.status_code!=206 or response.headers.get('content-range','')!=f"bytes {offset}-{spec['bytes']-1}/{spec['bytes']}"):raise ValueError('服务未返回正确续传范围；部分文件保留')
                                    elif not offset and response.status_code not in {200,206}:raise DownloadHTTPError(response.status_code)
                                    if response.status_code==206 and response.headers.get('content-range','')!=f"bytes {offset}-{spec['bytes']-1}/{spec['bytes']}":raise ValueError('服务返回的文件范围不一致，部分文件保留')
                                    job['state']='downloading'
                                    with part.open('ab' if offset else 'wb') as stream:
                                        for chunk in response.iter_bytes(64*1024):
                                            if self.cancel_event.is_set():raise InterruptedError('已暂停，已下载部分保留')
                                            if offset+len(chunk)>spec['bytes']:raise ValueError('下载超出固定文件大小')
                                            stream.write(chunk);offset+=len(chunk);job['received']=completed+offset;record()
                                        stream.flush();os.fsync(stream.fileno())
                                if offset!=spec['bytes']:raise httpx.ReadError('incomplete body')
                                break
                            except (httpx.TransportError,DownloadHTTPError,OSError) as error:
                                if self.cancel_event.is_set():raise InterruptedError('已暂停，部分文件保留')
                                reason=download_error(error);job.update(error=reason,received=completed+(part.stat().st_size if part.is_file() else 0))
                                if attempt==2 or isinstance(error,DownloadHTTPError) and error.status not in {408,429,500,502,503,504}:raise ValueError(reason+'；部分文件保留，可继续') from None
                                job.update(state='retrying',message=reason+'；'+str(attempt+1)+'/'+str(2)+'次重试，部分文件保留');record(True)
                                if self.cancel_event.wait(1 if attempt==0 else 2):raise InterruptedError('已暂停，部分文件保留')
                    job['state']='verifying';job['message']='正在核对SHA256';record(True)
                    if part.stat().st_size!=spec['bytes'] or digest(part,self.cancel_event)!=spec['sha256']:raise ValueError('文件未通过SHA256核验，未安装；请选择重新下载失败文件，坏件会保留')
                    with self.lock:
                        if self.cancel_event.is_set():raise InterruptedError('文件操作已停止，部分文件保留')
                        os.replace(part,target);completed+=spec['bytes'];job['received']=completed
                with self.lock:
                    if self.cancel_event.is_set():raise InterruptedError('文件操作已停止，部分文件保留')
                    installed=self._load('installed.json',[]);installed=[r for r in installed if r['id']!=model['id']]
                    installed.append({**{k:v for k,v in model.items() if k!='files'},'origin':'download','model_files':[{'path':str(self._file(model,s['name']).relative_to(self.context.control)),'sha256':s['sha256'],'bytes':s['bytes']} for s in model['files']]})
                    self._save(self.root/'installed.json',installed);job.update(state='completed',received=model['bytes'],message='下载及完整性核对完成；尚未切换模型');record(True)
        except InterruptedError as error:job.update(state='paused',message=str(error))
        except ValueError as error:job.update(state='failed',message=str(error))
        except Exception as error:job.update(state='paused' if self.cancel_event.is_set() else 'failed',error=download_error(error),message='已暂停，部分文件保留' if self.cancel_event.is_set() else download_error(error)+'；部分文件保留，可继续')
        finally:self.response=None;record(True)
    def import_existing(self,path,name='',thinking_mode='unverified',context_tokens=8192):
        source=Path(path)
        if not source.is_absolute() or source.suffix.lower()!='.gguf' or not source.is_file():raise ValueError('请选择实际本机GGUF文件的完整路径')
        if thinking_mode not in {'unverified','switchable','required','unsupported'} or type(context_tokens) is not int or not 1024<=context_tokens<=262144:raise ValueError('模型能力或上下文设置无效')
        with self.lock:
            if self.worker and self.worker.is_alive():raise ValueError('已有模型文件操作进行中，请先暂停')
            self.files.mkdir(parents=True,exist_ok=True);size=source.stat().st_size
            if shutil.disk_usage(self.files).free<size+64*1024*1024:raise ValueError('复制到模型目录的空间不足，原件保持不变')
            self.cancel_event=threading.Event();model={'id':'local-'+uuid.uuid4().hex,'revision':'local-import','name':name.strip()[:100] or source.stem,'bytes':size,'context_tokens':context_tokens,'max_output_tokens':context_tokens,'thinking_mode':thinking_mode}
            run_id=uuid.uuid4().hex;save(self.root/'download.json',{'run_id':run_id,'model_id':model['id'],'name':model['name'],'state':'downloading','received':0,'total':size,'message':'正在建立导入任务'})
            self.worker=threading.Thread(target=self._import,args=(source,model,run_id),daemon=True);self.worker.start()
            return {'state':'starting','model_id':model['id']}
    def _import(self,source,model,run_id=None):
        from .bounded_files import WindowsHandles
        import ctypes
        self.root.mkdir(parents=True,exist_ok=True);job={'run_id':run_id,'model_id':model['id'],'name':model['name'],'total':model['bytes'],'received':0,'state':'downloading','message':'正在导入已有GGUF；原件保持不变'}
        target=self._file(model,'model.gguf');part=target.with_suffix('.gguf.part');value=hashlib.sha256();last=0
        try:
            with FileLock(str(self.root/'download.lock'),timeout=0):
                handles=WindowsHandles();buffer=ctypes.create_string_buffer(1024*1024);count=handles.w.DWORD()
                with handles.pin(source.parent,()),handles.open(source) as handle,part.open('wb') as out:
                    while True:
                        if self.cancel_event.is_set():raise InterruptedError('导入已暂停，原件保持不变；可重新选择导入')
                        if not handles.api.ReadFile(handle,buffer,len(buffer),ctypes.byref(count),None):raise OSError('读取GGUF失败')
                        if not count.value:break
                        chunk=buffer.raw[:count.value]
                        if not job['received'] and chunk[:4]!=b'GGUF':raise ValueError('文件没有GGUF标记，未登记为模型')
                        if job['received']+len(chunk)>model['bytes']:raise ValueError('源文件大小变化，未登记')
                        out.write(chunk);value.update(chunk);job['received']+=len(chunk)
                        if time.monotonic()-last>.2:self._save(self.root/'download.json',job);last=time.monotonic()
                    out.flush();os.fsync(out.fileno())
                if job['received']!=model['bytes']:raise ValueError('源文件未完整读取')
                with self.lock:
                    if self.cancel_event.is_set():raise InterruptedError('导入已停止，原件保持不变')
                    os.replace(part,target);installed=self._load('installed.json',[]);installed.append({**model,'origin':'local_import','license':'本机导入，许可需自行核对','model_files':[{'path':str(target.relative_to(self.context.control)),'sha256':value.hexdigest(),'bytes':model['bytes']}]});self._save(self.root/'installed.json',installed);job.update(state='completed',message='已有GGUF已复制并核验；原件不变，尚未切换模型');self._save(self.root/'download.json',job)
        except (ValueError,InterruptedError) as error:job.update(state='paused' if self.cancel_event.is_set() else 'failed',message=str(error))
        except Exception:job.update(state='failed',message='导入未完成，请核对文件及空间；原件保持不变')
        finally:self._save(self.root/'download.json',job)
    def activate(self,model_id,*,context_tokens=8192):
        with self.lock:
            if type(context_tokens) is not int or not 1024<=context_tokens<=262144:raise ValueError('本机运行上下文须在1024至262144之间；更大窗口需另行配置并验证')
            old=self.runtime.config();installed=self._load('installed.json',[])
            if old.get('model_path') and not any(r['id']==old.get('model_id','existing') for r in installed):installed.append({'id':old.get('model_id','existing'),'name':old.get('model_name','已有本机模型'),'config':old,'origin':'existing','thinking_mode':old.get('thinking_mode','unverified')})
            row=next((r for r in installed if r['id']==model_id),None)
            if row is None:raise ValueError('模型尚未下载完整或登记')
            if row.get('config'):config={**row['config'],'enabled':True,'model_id':row['id']}
            else:
                if not old.get('server_path'):raise ValueError('本机推理引擎尚未配置，请先选择已有引擎或接入API；不会自动安装4B')
                files=row['model_files'];config={**old,'enabled':True,'model_id':row['id'],'model_name':row['name'],'model_path':files[0]['path'],'model_sha256':files[0]['sha256'],'model_files':files,'thinking_mode':row['thinking_mode'],'context_tokens':min(context_tokens,row['context_tokens']),'output_capacity':min(row['max_output_tokens'],context_tokens)}
            self.runtime.close();self._save(self.runtime.config_path,config);self._save(self.root/'installed.json',installed)
        for callback in self.on_change:callback()
        return self.status()
    def close(self):
        self.pause()
        if self.worker:self.worker.join(3)
