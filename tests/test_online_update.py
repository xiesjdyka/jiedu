import base64,hashlib,json,tempfile,unittest,zipfile
from pathlib import Path
from io import BytesIO
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from knowledge_bridge.online_update import OnlineUpdate
from knowledge_bridge.desktop_update import safe_member

class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.program=self.root/'program';(self.program/'knowledge_bridge').mkdir(parents=True)
        (self.program/'pyproject.toml').write_text('[project]\nversion="1.0.0"','utf-8')
        self.key=Ed25519PrivateKey.generate()
        (self.program/'knowledge_bridge/update-public-key.txt').write_bytes(base64.b64encode(self.key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)))
        (self.program/'knowledge_bridge/update-channel.json').write_text(json.dumps({'test_only':True,'manifest_url':'https://updates.example/channel','asset_hosts':['updates.example']}),'utf-8')
        self.updater=OnlineUpdate(self.program,self.root/'state')
        files={'knowledge_bridge/fixture.py':hashlib.sha256(b'print(1)').hexdigest()}
        raw=json.dumps({'format':'jiedu-program-update-v1','version':'1.1.0','files':files}).encode()
        output=BytesIO()
        with zipfile.ZipFile(output,'w') as z:
            z.writestr('manifest.json',raw);z.writestr('signature.txt',base64.b64encode(self.key.sign(raw)))
            z.writestr('program/knowledge_bridge/fixture.py',b'print(1)')
        self.package=output.getvalue()
        self.record={'format':'jiedu-update-channel-v1','test_only':True,'expires_at':(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),'version':'1.1.0','url':'https://updates.example/package','size':len(self.package),'sha256':hashlib.sha256(self.package).hexdigest()}
    def envelope(self,**changes):
        raw=json.dumps({**self.record,**changes}).encode()
        return json.dumps({'payload':base64.b64encode(raw).decode(),'signature':base64.b64encode(self.key.sign(raw)).decode()}).encode()
    def test_verified_download_never_changes_workspace_or_program(self):
        private=self.root/'source.md';private.write_text('synthetic')
        with patch.object(self.updater,'fetch',side_effect=[self.envelope(),self.package]):
            self.assertEqual(self.updater.check()['state'],'available')
            self.assertEqual(self.updater.download()['state'],'downloaded')
        self.assertEqual(private.read_text(),'synthetic');self.assertFalse((self.program/'knowledge_bridge/fixture.py').exists())
    def test_corrupt_payload_cannot_be_downloaded(self):
        with patch.object(self.updater,'fetch',side_effect=[self.envelope(),b'corrupted']):
            self.updater.check()
            with self.assertRaises(ValueError):self.updater.download()
        self.assertFalse((self.updater.root/'verified.jiedu').exists())
    def test_signature_expiry_host_and_downgrade(self):
        for changes in [{'expires_at':'2000-01-01T00:00:00Z'},{'url':'https://attacker.example/file'}]:
            with self.assertRaises(ValueError):self.updater.verify(self.envelope(**changes))
        envelope=json.loads(self.envelope());envelope['signature']=base64.b64encode(b'x'*64).decode()
        with self.assertRaises(ValueError):self.updater.verify(json.dumps(envelope).encode())
        with patch.object(self.updater,'fetch',return_value=self.envelope(version='0.9.0')):
            self.assertEqual(self.updater.check()['state'],'current')
            with self.assertRaisesRegex(ValueError,'没有可安装'):self.updater.download()
    def test_unpublished_channel_is_not_reported_as_latest(self):
        (self.program/'knowledge_bridge/update-channel.json').write_text('{}')
        with patch.object(self.updater,'fetch') as network:
            self.assertEqual(self.updater.check()['state'],'unpublished');network.assert_not_called()
    def test_windows_path_aliases_are_rejected(self):
        for path in ['knowledge_bridge/CON.py','knowledge_bridge/file.py.','knowledge_bridge/../secret','knowledge_bridge/a:b.py']:
            self.assertFalse(safe_member(path),path)

    def test_redirects_are_bounded_and_pinned(self):
        import httpx
        def handler(request):
            if request.url.path=='/first':return httpx.Response(302,headers={'location':'/ok'})
            if request.url.path=='/bad':return httpx.Response(302,headers={'location':'http://updates.example/ok'})
            if request.url.path=='/foreign':return httpx.Response(302,headers={'location':'https://foreign.example/ok'})
            return httpx.Response(200,content=b'ok')
        for path in ['first','bad','foreign']:
            self.updater.client=httpx.Client(transport=httpx.MockTransport(handler))
            if path=='first':self.assertEqual(self.updater.fetch('https://updates.example/'+path,2),b'ok')
            else:
                with self.assertRaises(ValueError):self.updater.fetch('https://updates.example/'+path,2)

    def _complete_installer(self, content):
        self.record.update(kind='complete-installer',version='1.1.3',
                           url='https://updates.example/Jiedu-1.1.3-windows-x64-setup.exe',
                           installer_name='Jiedu-1.1.3-windows-x64-setup.exe',
                           size=len(content),sha256=hashlib.sha256(content).hexdigest())
        with patch.object(self.updater,'fetch',return_value=self.envelope()):
            self.assertEqual('available',self.updater.check()['state'])

    def test_complete_installer_resumes_after_interrupted_stream(self):
        import httpx
        content=b'MZ'+b'synthetic-installer-bytes'*7000
        prefix=65536
        self._complete_installer(content)
        seen=[]
        class Interrupted(httpx.SyncByteStream):
            def __iter__(self):
                yield content[:prefix]
                raise httpx.ReadError('synthetic connection loss')
        def handler(request):
            seen.append(request.headers.get('range'))
            if len(seen)==1:return httpx.Response(200,stream=Interrupted())
            return httpx.Response(206,headers={'Content-Range':f'bytes {prefix}-{len(content)-1}/{len(content)}'},
                                  content=content[prefix:])
        self.updater.client=httpx.Client(transport=httpx.MockTransport(handler))
        with patch.object(self.updater,'_inspect',return_value={}), \
             patch.object(self.updater.cancel_event,'wait',return_value=False):
            self.assertEqual('downloaded',self.updater.download()['state'])
        self.assertEqual([None,f'bytes={prefix}-'],seen)
        self.assertEqual(content,(self.updater.root/'verified-installer.exe').read_bytes())

    def test_complete_installer_accepts_complete_bytes_after_disconnect(self):
        import httpx
        content=b'MZ'+b'a'*(2*65536-2)
        self._complete_installer(content)
        calls=[]
        class CompleteThenDisconnected(httpx.SyncByteStream):
            def __iter__(self):
                yield content
                raise httpx.ReadError('connection closed after all bytes')
        def handler(request):
            calls.append(request.headers.get('range'))
            if len(calls)<4:raise httpx.ReadError('earlier transient connection loss')
            return httpx.Response(200,stream=CompleteThenDisconnected())
        self.updater.client=httpx.Client(transport=httpx.MockTransport(handler))
        with patch.object(self.updater,'_inspect',return_value={}), \
             patch.object(self.updater.cancel_event,'wait',return_value=False):
            self.assertEqual('downloaded',self.updater.download()['state'])
        self.assertEqual([None]*4,calls)
        self.assertEqual(content,(self.updater.root/'verified-installer.exe').read_bytes())

    def test_complete_installer_rejects_ignored_or_invalid_range_and_bad_hash(self):
        import httpx
        content=b'MZ'+b'synthetic-installer-bytes'*7000
        prefix=65536
        for mode in ('ignored','wrong_start','wrong_length','bad_hash'):
            with self.subTest(mode=mode):
                (self.updater.root/'verified-installer.exe').unlink(missing_ok=True)
                self._complete_installer(content)
                class Interrupted(httpx.SyncByteStream):
                    def __iter__(self):
                        yield content[:prefix]
                        raise httpx.ReadError('synthetic connection loss')
                requests=[]
                def handler(request):
                    requests.append(request.headers.get('range'))
                    if mode=='bad_hash':return httpx.Response(200,content=b'XX'+content[2:])
                    if len(requests)==1:return httpx.Response(200,stream=Interrupted())
                    if mode=='ignored':return httpx.Response(200,content=content)
                    if mode=='wrong_start':
                        return httpx.Response(206,headers={'Content-Range':f'bytes 0-{len(content)-1}/{len(content)}'},
                                              content=content[prefix:])
                    return httpx.Response(206,headers={'Content-Range':f'bytes {prefix}-{len(content)-1}/{len(content)}'},
                                          content=content[prefix:-1])
                self.updater.client=httpx.Client(transport=httpx.MockTransport(handler))
                with patch.object(self.updater,'_inspect',return_value={}), \
                     patch.object(self.updater.cancel_event,'wait',return_value=False):
                    with self.assertRaises(ValueError):
                        self.updater.download()
                self.assertFalse((self.updater.root/'verified-installer.exe').exists())
                self.assertFalse(list(self.updater.root.glob('*.partial')))
                self.updater.client.close()
                self.updater.client=None

    def test_complete_installer_cancel_and_retry_bound(self):
        import httpx
        content=b'MZ'+b'synthetic-installer-bytes'
        self._complete_installer(content)
        calls=[]
        def unavailable(request):
            calls.append(request)
            raise httpx.ReadError('synthetic persistent loss')
        self.updater.client=httpx.Client(transport=httpx.MockTransport(unavailable))
        with patch.object(self.updater.cancel_event,'wait',return_value=False):
            with self.assertRaises(Exception):self.updater.download()
        self.assertEqual(4,len(calls))
        self.assertFalse((self.updater.root/'verified-installer.exe').exists())
        self.updater.client.close()
        self.updater.client=None
        self.updater.cancel_event.set()
        self.updater.worker=type('Worker',(),{'is_alive':lambda self:True})()
        with patch.object(self.updater,'_stream') as stream:
            with self.assertRaisesRegex(ValueError,'取消'):self.updater.download()
            stream.assert_not_called()

    def test_cancelled_update_is_reported_as_cancelled(self):
        class Gate:
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def release(self,force=False):pass
        self.updater.operation_lock=Gate()
        self.updater.cancel_event.set()
        with patch.object(self.updater,'check',side_effect=ValueError('更新已取消，原版本保持不变')):
            self.updater._run()
        self.assertEqual('cancelled',self.updater._read('workflow.json')['state'])
