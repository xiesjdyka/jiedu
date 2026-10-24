"""Signed checks and one explicit serialized update workflow; no telemetry."""
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid
from urllib.parse import urlsplit, urljoin

import httpx
from packaging.version import Version
from .desktop_update import inspect_package, MAX_BYTES
from .full_installer_update import MAX_INSTALLER_BYTES, digest, inspect_installer


def version(program):
    from .release_version import version_info
    return version_info(program)['product_version']


class ChannelUnavailable(ValueError):
    pass


def public_url(url, *, test_only=False):
    if not isinstance(url, str) or url != url.strip() or any(ord(c) < 33 for c in url):
        raise ValueError('更新地址必须是有效的 HTTPS 发布地址')
    p = urlsplit(url)
    host = (p.hostname or '').lower()
    if (p.scheme != 'https' or not host or p.username is not None or p.password is not None
            or p.fragment or '\\' in url or re.search(r'(?i)(<|>|\{|\}|placeholder|changeme|replace-me)', url)):
        raise ValueError('更新地址必须是有效的 HTTPS 发布地址')
    if not test_only and (host == 'localhost' or host.endswith(('.example', '.test', '.invalid', '.localhost', '.local'))
                         or host in {'example.com', 'example.net', 'example.org', '127.0.0.1', '::1'}):
        raise ValueError('正式更新渠道不能使用占位或本机地址')
    return url


class OnlineUpdate:
    def __init__(self, program, state, *, client=None, busy=None, quiesce=None,
                 resume=None, handoff=None, idle_interval=1.0, check_interval=43200):
        self.program = Path(program).resolve()
        self.root = Path(state).resolve() / 'software-update'
        self.client = client
        self.busy, self.quiesce, self.resume, self.handoff = busy, quiesce, resume, handoff
        self.idle_interval = idle_interval
        self.lock = threading.RLock()
        self.state_lock = threading.RLock()
        self.worker = None
        self.cancel_event = threading.Event()
        self._resumed_helper = None
        self.operation_lock = None
        self.recovery_requested = False
        self.check_interval = max(900, float(check_interval))
        self.check_worker = None
        self.check_stop = threading.Event()
        self.check_wake = threading.Event()

    def _attempt(self, success):
        previous = self._read('last-attempt.json')
        failures = 0 if success else min(8, previous.get('failures', 0) + 1)
        delay = self.check_interval if success else min(self.check_interval, 900 * 2 ** (failures - 1))
        self._save('last-attempt.json', {'attempt_at': time.time(), 'next_check_at': time.time() + delay,
                                        'failures': failures})

    def _helper_outcome(self):
        helper = self._read('helper.json')
        if not re.fullmatch('[0-9a-f]{32}', helper.get('token', '')) or not helper.get('work'):
            return {}
        from .full_installer_update import no_links
        try:
            work = no_links(helper['work'])
            expected = self.program.parent.parent.parent / ('.jiedu-update-' + helper['token'])
            if work != expected.resolve():
                return {}
            outcome = json.loads((work / 'result.json').read_text('utf-8'))
            return outcome if outcome.get('token') == helper['token'] else {}
        except FileNotFoundError:
            if helper.get('helper_pid') and 'work' in locals() and work.is_dir():
                return {'state':'waiting_exit', 'token':helper['token'], 'message':'正在等待安全退出并交接更新。'}
            return {}
        except (OSError, ValueError):
            if 'work' in locals() and work.is_dir():
                return {'state':'waiting_exit','token':helper['token'],'message':'更新助手回执暂不可读，已阻止并发更新；请等待或检查恢复材料。'}
            return {}

    def _clear_finished_for_check(self):
        if self.worker and self.worker.is_alive():
            return
        outcome = self._helper_outcome()
        if outcome.get('state') in {'waiting_exit','applying','installed','finalizing','recovery_required'}:
            return
        if outcome:
            self._save('last-outcome.json', outcome)
            self._save('helper.json', {})
        self._save('workflow.json', {})

    def channel(self):
        file = self.program / 'knowledge_bridge/update-channel.json'
        return json.loads(file.read_text('utf-8')) if file.exists() else {}

    def _read(self, name):
        try:
            with self.state_lock:
                value = json.loads((self.root / name).read_text('utf-8'))
            return value if isinstance(value, dict) else {}
        except (ValueError, OSError):
            return {}

    def status(self):
        current = version(self.program)
        if not self.channel().get('manifest_url'):
            return dict(state='unpublished', version=current,
                        message='此版本尚未发布在线更新渠道；配置并发布后可检查。')
        result = self._read('last-check.json') or dict(state='not_checked', message='将自动检查更新；安装需要你点击开始。')
        job = self._read('workflow.json')
        if job:
            result.update(job)
        helper = self._read('helper.json')
        outcome = self._helper_outcome()
        if outcome and (not self.worker or not self.worker.is_alive() or job.get('token') == outcome.get('token')):
            result.update(outcome)
            if outcome.get('state') in {'failed', 'rolled_back'} and self.resume and self._resumed_helper != helper['token']:
                self.resume()
                self._resumed_helper = helper['token']
        return {**result, 'version': current,
                'remind_at': self._read('reminder.json').get('remind_at'),
                'running': bool((self.worker and self.worker.is_alive()) or outcome.get('state') in {'waiting_exit','applying','installed','finalizing'})}

    def _save(self, name, value):
        with self.state_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.root / (name + '.' + uuid.uuid4().hex + '.tmp')
            tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf-8')
            try:
                for attempt in range(5):
                    try:
                        os.replace(tmp, self.root / name)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(.01 * (attempt + 1))
            finally:
                tmp.unlink(missing_ok=True)

    def _phase(self, state, message, **values):
        with self.state_lock:
            record = {**self._read('workflow.json'), 'state': state, 'phase': state,
                      'message': message, 'updated_at': datetime.now(timezone.utc).isoformat(), **values}
            self._save('workflow.json', record)
            return record

    def _claim_handoff(self, message, token, request_path=None):
        """Make cancellation and the irreversible handoff state one decision."""
        with self.state_lock:
            if self.cancel_event.is_set():
                if request_path is not None:
                    pending = Path(request_path).resolve()
                    if pending.parent != self.root or pending.name != 'handoff-' + token + '.json':
                        raise ValueError('更新交接请求路径无效，未启动安装')
                    pending.unlink(missing_ok=True)
                self._phase('cancelled', '更新已取消，原版本保持不变。')
                return False
            self._phase('handoff_ready', message, token=token)
            return True

    def _stream(self, url, limit, consume, *, offset=0, deadline=None):
        config = self.channel()
        test_only = config.get('test_only') is True
        public_url(url, test_only=test_only)
        allowed = {urlsplit(url).hostname, *config.get('asset_hosts', [])}
        started = time.monotonic()
        client = self.client or httpx.Client(timeout=httpx.Timeout(30, connect=8), follow_redirects=False)
        try:
            if offset < 0 or offset >= limit:
                raise ValueError('续传位置无效')
            headers={'Accept': 'application/octet-stream'}
            if offset:headers['Range']=f'bytes={offset}-'
            for _ in range(5):
                with client.stream('GET', url, headers=headers) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        url = public_url(urljoin(url, response.headers.get('location', '')), test_only=test_only)
                        if urlsplit(url).hostname not in allowed:
                            raise ValueError('更新跳转到了未经配置的下载站点')
                        continue
                    if response.status_code in {401, 403, 404}:
                        raise ChannelUnavailable('更新渠道尚未发布或暂不可访问，请稍后重试。')
                    expected_end=None
                    if offset:
                        if response.status_code != 206:
                            raise ValueError('下载站点未按请求续传，未使用返回内容')
                        match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.headers.get('Content-Range',''))
                        if not match or int(match[1])!=offset or int(match[3])!=limit or not offset<=int(match[2])<limit:
                            raise ValueError('续传范围与已下载文件不一致')
                        expected_end=int(match[2])
                    elif response.status_code != 200:
                        raise ChannelUnavailable('更新服务器暂时不可用，请稍后重试。')
                    total=offset
                    for piece in response.iter_bytes(65536):
                        if self.cancel_event.is_set() and self.worker and self.worker.is_alive():
                            raise ValueError('更新已取消，原版本保持不变')
                        if time.monotonic() - started > 1800 or (deadline and time.monotonic()>deadline):
                            raise ChannelUnavailable('更新下载超时，请重试。')
                        total+=len(piece)
                        if total>limit or (expected_end is not None and total>expected_end+1):
                            raise ValueError('更新下载超过允许大小或续传范围')
                        consume(piece,total)
                    if expected_end is not None and total!=expected_end+1:
                        raise ChannelUnavailable('续传响应不完整，请稍后重试。')
                    return total
            raise ValueError('更新地址跳转次数过多')
        except httpx.HTTPError:
            raise ChannelUnavailable('无法连接更新服务，请检查网络后重试。') from None
        finally:
            if self.client is None:client.close()

    def fetch(self, url, limit):
        body = bytearray()
        self._stream(url, limit, lambda piece, total: body.extend(piece))
        return bytes(body)

    def verify(self, raw):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        try:
            if len(raw) > 128 * 1024:
                raise ValueError()
            envelope = json.loads(raw)
            payload = base64.b64decode(envelope['payload'], validate=True)
            signature = base64.b64decode(envelope['signature'], validate=True)
            key = base64.b64decode((self.program / 'knowledge_bridge/update-public-key.txt').read_text('ascii').strip(), validate=True)
            Ed25519PublicKey.from_public_bytes(key).verify(signature, payload)
            record = json.loads(payload)
            if record['format'] != 'jiedu-update-channel-v1':
                raise ValueError()
            expiry = datetime.fromisoformat(record['expires_at'].replace('Z', '+00:00'))
            if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                raise ValueError()
            Version(record['version'])
            kind = record.get('kind', 'program-only')
            if kind not in {'program-only', 'complete-installer'}:
                raise ValueError()
            maximum = MAX_INSTALLER_BYTES if kind == 'complete-installer' else MAX_BYTES
            if type(record['size']) is not int or not 0 < record['size'] <= maximum:
                raise ValueError()
            if not re.fullmatch('[0-9a-f]{64}', record['sha256']):
                raise ValueError()
            config = self.channel()
            test_only = config.get('test_only') is True
            if record.get('test_only', False) != test_only:
                raise ValueError()
            if config.get('channel') == 'stable' and Version(record['version']).is_prerelease:
                raise ValueError()
            public_url(record['url'], test_only=test_only)
            if urlsplit(record['url']).hostname not in config.get('asset_hosts', []):
                raise ValueError()
            if kind == 'complete-installer' and record.get('installer_name') != 'Jiedu-' + record['version'] + '-windows-x64-setup.exe':
                raise ValueError()
            return {**record, 'kind': kind}
        except Exception:
            raise ValueError('更新信息校验失败或已过期，未安装任何内容') from None

    def check(self):
        with self.lock:
            config = self.channel()
            if not config.get('manifest_url'):
                return self.status()
            self._attempt(False)
            try:
                raw = self.fetch(config['manifest_url'], 128 * 1024)
                record = self.verify(raw)
            except ChannelUnavailable as error:
                result = dict(state='unavailable', version=version(self.program), message=str(error),
                              checked_at=datetime.now(timezone.utc).isoformat())
                self._save('last-check.json', result)
                self._clear_finished_for_check()
                return result
            newer = Version(record['version']) > Version(version(self.program))
            result = dict(state='available' if newer else 'current', version=version(self.program),
                          latest=record['version'], kind=record['kind'], checked_at=datetime.now(timezone.utc).isoformat(),
                          message='有新版本，点击开始后将下载、核验并在空闲时更新。' if newer else '当前版本无需更新。',
                          notes=str(record.get('notes', ''))[:4000])
            if record['kind'] == 'complete-installer':
                result.update(authenticode_asserted=False,
                              windows_signature_notice='此安装器依靠签名发布信息和 SHA256 核验来源；未宣称具有 Windows 信誉签名。')
            self._save('channel-envelope.json', json.loads(raw))
            self._save('last-check.json', result)
            self._attempt(True)
            reminder = self._read('reminder.json')
            if reminder and reminder.get('latest') != record['version']:
                self._save('reminder.json', {})
            self._clear_finished_for_check()
            return result

    def _record(self):
        path = self.root / 'channel-envelope.json'
        if not path.exists():
            raise ValueError('请先检查更新')
        record = self.verify(path.read_bytes())
        if Version(record['version']) <= Version(version(self.program)):
            raise ValueError('没有可安装的新版本')
        return record

    def _artifact(self, record):
        return self.root / ('verified-installer.exe' if record['kind'] == 'complete-installer' else 'verified.jiedu')

    def _inspect(self, path, record):
        if record['kind'] == 'complete-installer':
            return inspect_installer(path, record)
        manifest = inspect_package(path, self.program)
        if Version(manifest['version']) != Version(record['version']):
            raise ValueError('更新版本与发布信息不一致')
        return manifest

    def download(self):
        with self.lock:
            record = self._record()
            pending = self.root / ('download-' + uuid.uuid4().hex + '.partial')
            self.root.mkdir(parents=True, exist_ok=True)
            try:
                if record['kind'] == 'program-only':
                    pending.write_bytes(self.fetch(record['url'], record['size']))
                else:
                    with pending.open('xb') as output:
                        last = [0.0]
                        def consume(piece, total):
                            output.write(piece)
                            if time.monotonic() - last[0] >= .25:
                                self._phase('downloading', '正在下载完整安装器…', downloaded_bytes=total, total_bytes=record['size'])
                                last[0] = time.monotonic()
                        deadline=time.monotonic()+600
                        for attempt in range(4):
                            if self.cancel_event.is_set() and self.worker and self.worker.is_alive():
                                raise ValueError('更新已取消，原版本保持不变')
                            if output.tell()==record['size']:break
                            try:
                                total=self._stream(record['url'],record['size'],consume,
                                                   offset=output.tell(),deadline=deadline)
                            except ChannelUnavailable:
                                if self.cancel_event.is_set() and self.worker and self.worker.is_alive():
                                    raise ValueError('更新已取消，原版本保持不变')
                                if output.tell()==record['size']:break
                                if attempt==3 or time.monotonic()>deadline:raise
                                self._phase('downloading','连接中断，正在续传完整安装器…',
                                            downloaded_bytes=output.tell(),total_bytes=record['size'])
                                if self.cancel_event.wait(min(2**attempt,4)) and self.worker and self.worker.is_alive():
                                    raise ValueError('更新已取消，原版本保持不变')
                                continue
                            if total==record['size']:break
                            if attempt==3:raise ChannelUnavailable('更新下载不完整，请检查网络后重试。')
                        else:
                            raise ChannelUnavailable('更新下载不完整，请检查网络后重试。')
                        output.flush()
                        os.fsync(output.fileno())
                if pending.stat().st_size != record['size'] or digest(pending) != record['sha256']:
                    raise ValueError('更新下载不完整或校验失败，请重新下载')
                self._inspect(pending, record)
                os.replace(pending, self._artifact(record))
            finally:
                pending.unlink(missing_ok=True)
            result = {**self._read('last-check.json'), 'state': 'downloaded', 'kind': record['kind'],
                      'message': '已下载并校验来源与文件完整性，等待空闲后更新。'}
            self._save('last-check.json', result)
            return result

    def _default_handoff(self, request):
        from .full_installer_update import start_helper
        return start_helper(request)

    def apply(self):
        """Legacy API also uses the guarded asynchronous workflow."""
        return self.start()

    def start(self, *, recovery=False):
        with self.lock:
            if not self.channel().get('manifest_url'):
                return self.status()
            if self.worker and self.worker.is_alive():
                return self.status()
            outcome = self._helper_outcome()
            if outcome.get('state') in {'waiting_exit','applying','installed','finalizing'}:
                return self.status()
            if outcome.get('state') == 'recovery_required' and not recovery:
                raise ValueError('请先恢复或审阅失败更新，不能覆盖唯一回退副本')
            if recovery and outcome.get('state') != 'recovery_required':
                raise ValueError('当前没有待恢复的完整安装事务')
            from filelock import FileLock, Timeout
            self.root.mkdir(parents=True, exist_ok=True)
            operation = FileLock(str(self.root / 'workflow.lock'), thread_local=False)
            try:
                operation.acquire(timeout=0)
            except Timeout:
                return self.status()
            self.operation_lock = operation
            self.recovery_requested = recovery
            if not recovery:
                self._save('helper.json', {})
            self.cancel_event.clear()
            self._save('workflow.json', dict(job_id=uuid.uuid4().hex, state='queued', phase='queued', message='正在准备更新…'))
            self.worker = threading.Thread(target=self._run, name='signed-software-update', daemon=True)
            self.worker.start()
            return self.status()

    def _run(self):
        from filelock import FileLock, Timeout
        gate_started = False
        handed_off = False
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.operation_lock:
                if not self.recovery_requested:
                    self._phase('checking', '正在检查更新…')
                    checked = self.check()
                    if checked['state'] != 'available':
                        self._phase(checked['state'], checked['message'])
                        return
                    record = self._record()
                    if record['kind'] == 'complete-installer':
                        from .full_installer_update import installation_owner, verify_installation_inventory
                        installation_owner(self.program)
                        verify_installation_inventory(self.program)
                    self._phase('downloading', '正在下载并校验更新…', latest=record['version'], kind=record['kind'])
                    artifact = self._artifact(record)
                    if not artifact.is_file() or artifact.stat().st_size != record['size'] or digest(artifact) != record['sha256']:
                        self.download()
                    self._phase('verifying', '正在复核签名、版本和安装归属…')
                    self._inspect(artifact, record)
                while not self.cancel_event.is_set():
                    reasons = list(self.busy()) if self.busy else ['尚未接入运行任务的空闲核验']
                    if not reasons and self.quiesce:
                        gate_started = True
                        if self.quiesce() and not list(self.busy()):
                            break
                    self._phase('waiting_idle', '正在等待当前任务自然结束；不会强制关闭。', busy=reasons)
                    self.cancel_event.wait(self.idle_interval)
                if self.cancel_event.is_set():
                    self._phase('cancelled', '更新已取消，原版本保持不变。')
                    return
                if self.recovery_requested:
                    if not self._claim_handoff('正在安全退出并恢复上一个完整版本。',
                                               self._read('helper.json')['token']):
                        return
                    from .full_installer_update import start_recovery
                    start_recovery(self.program, self.root)
                    handed_off = True
                    self._phase('restarting', '正在恢复上一个版本并重启，失败材料将保留。')
                    return
                record = self._record()
                self._inspect(artifact, record)
                from .full_installer_update import prepare_handoff
                request = prepare_handoff(self.program, self.root, record, artifact)
                if not self._claim_handoff('当前任务已结束，正在交接安装并准备重启。',
                                           request['token'], request['request_path']):
                    return
                (self.handoff or self._default_handoff)(request)
                handed_off = True
                self._phase('restarting', '正在安全退出并安装更新，完成后自动重启。')
        except Timeout:
            self._phase('failed', '另一更新任务正在运行，请等待其完成。')
        except Exception as error:
            if self.cancel_event.is_set() and str(error).startswith('更新已取消'):
                self._phase('cancelled', '更新已取消，原版本保持不变。')
            else:
                self._phase('failed', str(error)[:800], error=type(error).__name__)
        finally:
            if gate_started and not handed_off and self.resume:
                self.resume()
            if self.operation_lock:
                self.operation_lock.release(force=True)
                self.operation_lock = None

    def remind(self, seconds=86400):
        seconds = max(60, min(7 * 86400, int(seconds)))
        self._save('reminder.json', {'remind_at': time.time() + seconds, 'latest': self._read('last-check.json').get('latest')})
        return self.status()

    def startup_check(self):
        """One low-frequency scheduler, including long-running tray sessions."""
        with self.lock:
            if not self.check_worker or not self.check_worker.is_alive():
                self.check_stop.clear()
                self.check_worker = threading.Thread(target=self._scheduled_checks,
                    name='software-update-check-scheduler', daemon=True)
                self.check_worker.start()
            self.check_wake.set()
        return self.status()

    def _scheduled_checks(self):
        while not self.check_stop.is_set():
            self.check_wake.clear()
            if not self.channel().get('manifest_url'):
                delay = self.check_interval
            elif ((self.worker and self.worker.is_alive()) or
                  self._helper_outcome().get('state') in {'waiting_exit','applying','installed','finalizing','recovery_required'}):
                delay = 60
            else:
                attempt = self._read('last-attempt.json')
                due = max(attempt.get('next_check_at', 0), self._read('reminder.json').get('remind_at', 0))
                if not attempt:
                    try:
                        stamp = self._read('last-check.json').get('checked_at')
                        if stamp:
                            due = max(due, datetime.fromisoformat(stamp.replace('Z','+00:00')).timestamp() + self.check_interval)
                    except (ValueError, TypeError):
                        pass
                delay = max(0, due - time.time())
                if delay <= 0:
                    try:
                        self.check()
                    except Exception as error:
                        self._save('last-check.json', {'state':'failed','message':str(error)[:800]})
                    # A mocked or unavailable implementation must not busy-loop.
                    delay = max(60, self._read('last-attempt.json').get('next_check_at', time.time()+self.check_interval) - time.time())
            self.check_wake.wait(delay)

    def cancel(self):
        with self.state_lock:
            state = self._read('workflow.json').get('state')
            if state in {'handoff_ready', 'restarting'}:
                raise ValueError('更新已交接安装，请等待完成；不要关闭安装进程')
            self.cancel_event.set()
            if self.worker and self.worker.is_alive():
                self._phase(state or 'queued', '已接收取消请求，正在完成当前安全检查。',
                            cancel_requested=True)
        return self.status()

    def recover(self):
        return self.start(recovery=True)

    def close(self):
        self.check_stop.set()
        self.check_wake.set()
        if self.check_worker and self.check_worker is not threading.current_thread():
            self.check_worker.join(timeout=1)
        with self.state_lock:
            if self._read('workflow.json').get('state') not in {'handoff_ready', 'restarting'}:
                self.cancel_event.set()
