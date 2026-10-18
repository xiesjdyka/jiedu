"""Offline synthetic tests: no installer execution, real key, service or network."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from knowledge_bridge.online_update import OnlineUpdate, public_url
from knowledge_bridge.full_installer_update import (installation_owner, inspect_installer,
    prepare_handoff, validate_handoff, HANDOFF_FORMAT, HELPER_SCRIPT,
    check_workspace_boundary, retention_plan, tree_inventory, verify_installation_inventory,
    INVENTORY_FORMAT)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / 'app'
        self.program = self.target / '.jiedu/program'
        (self.program / 'knowledge_bridge').mkdir(parents=True)
        (self.program / 'VERSION').write_text('1.0.0', 'ascii')
        (self.program / 'pyproject.toml').write_text('[project]\nversion="1.0.0"', 'utf-8')
        (self.target / '界渡.exe').write_bytes(b'MZ synthetic native')
        (self.target / 'jiedu-install.txt').write_text('jiedu-consumer-install-v1', 'ascii')
        (self.program.parent / 'package.json').write_text(json.dumps({'format_version': 1,
            'layout': 'jiedu-application-v1', 'program': 'program'}), 'utf-8')
        self.key = Ed25519PrivateKey.generate()
        (self.program / 'knowledge_bridge/update-public-key.txt').write_bytes(base64.b64encode(self.key.public_key().public_bytes_raw()))
        self.config = {'test_only': True, 'manifest_url': 'https://updates.example/channel', 'asset_hosts': ['updates.example']}
        (self.program / 'knowledge_bridge/update-channel.json').write_text(json.dumps(self.config), 'utf-8')
        self.updater = OnlineUpdate(self.program, self.root / 'state', idle_interval=.005)
        self.addCleanup(self.updater.close)
        files = {'VERSION': b'1.1.0', 'pyproject.toml': b'[project]\nversion="1.1.0"', 'static/app.js': b'new'}
        raw = json.dumps({'format': 'jiedu-program-update-v1', 'version': '1.1.0',
                          'files': {name: hashlib.sha256(body).hexdigest() for name, body in files.items()}}).encode()
        output = BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('manifest.json', raw)
            archive.writestr('signature.txt', base64.b64encode(self.key.sign(raw)))
            for name, body in files.items():
                archive.writestr('program/' + name, body)
        self.package = output.getvalue()
        self.record = {'format': 'jiedu-update-channel-v1', 'kind': 'program-only',
            'test_only': True, 'version': '1.1.0', 'url': 'https://updates.example/package',
            'size': len(self.package), 'sha256': hashlib.sha256(self.package).hexdigest(),
            'expires_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}

    def envelope(self, record=None):
        raw = json.dumps(record or self.record).encode()
        return json.dumps({'payload': base64.b64encode(raw).decode(),
                           'signature': base64.b64encode(self.key.sign(raw)).decode()}).encode()

    def registry(self):
        return [{'scope': 'user', 'location': str(self.target)}]

    def join(self):
        self.updater.worker.join(3)
        self.assertFalse(self.updater.worker.is_alive())

    def wait_state(self, state):
        until = time.monotonic() + 3
        while time.monotonic() < until:
            if self.updater.status().get('state') == state:
                return
            time.sleep(.005)
        self.fail('workflow did not reach ' + state)

    def test_start_serial_waits_busy_then_handoff_once_without_mutating_program(self):
        idle = threading.Event()
        sent = []
        self.updater.busy = lambda: [] if idle.is_set() else ['synthetic request']
        self.updater.quiesce = lambda: True
        self.updater.handoff = lambda request: sent.append(request)
        with patch.object(self.updater, 'fetch', side_effect=[self.envelope(), self.package]), \
             patch('knowledge_bridge.full_installer_update.registrations', self.registry):
            first = self.updater.start()
            self.wait_state('waiting_idle')
            second = self.updater.start()
            self.assertEqual(first['job_id'], second['job_id'])
            self.assertEqual(sent, [])
            idle.set()
            self.join()
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.updater.status()['state'], 'restarting')
        self.assertFalse((self.program / 'static/app.js').exists())
        self.assertFalse(sent[0]['force_stop_allowed'])

    def test_cancel_during_draining_resumes_gate_and_keeps_old_version(self):
        resumed = []
        self.updater.busy = lambda: []
        self.updater.quiesce = lambda: False
        self.updater.resume = lambda: resumed.append(True)
        self.updater.handoff = lambda request: self.fail('cancelled handoff')
        with patch.object(self.updater, 'fetch', side_effect=[self.envelope(), self.package]):
            self.updater.start()
            self.wait_state('waiting_idle')
            self.updater.cancel()
            self.join()
        self.assertEqual(resumed, [True])
        self.assertEqual(self.updater.status()['state'], 'cancelled')
        self.assertEqual((self.program / 'VERSION').read_text(), '1.0.0')

    def test_cancel_before_handoff_commit_removes_request_and_after_commit_is_rejected(self):
        entered,release=threading.Event(),threading.Event()
        resumed=[]
        request_path=self.updater.root/('handoff-'+'a'*32+'.json')
        def slow_prepare(*args):
            request_path.write_text('{}','utf-8')
            entered.set()
            self.assertTrue(release.wait(3))
            return {'token':'a'*32,'request_path':str(request_path)}
        self.updater.busy=lambda: []
        self.updater.quiesce=lambda: True
        self.updater.resume=lambda: resumed.append(True)
        self.updater.handoff=lambda request:self.fail('accepted cancellation launched helper')
        with patch.object(self.updater,'fetch',side_effect=[self.envelope(),self.package]), \
             patch('knowledge_bridge.full_installer_update.prepare_handoff',side_effect=slow_prepare):
            self.updater.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertTrue(self.updater.cancel()['cancel_requested'])
            finally:
                release.set()
            self.join()
        self.assertEqual(self.updater.status()['state'],'cancelled')
        self.assertFalse(request_path.exists())
        self.assertEqual(resumed,[True])
        self.updater.cancel_event.clear()
        self.updater._phase('verifying','synthetic precommit')
        self.assertTrue(self.updater._claim_handoff('synthetic handoff','b'*32))
        with self.assertRaisesRegex(ValueError,'已交接安装'):
            self.updater.cancel()
        self.assertEqual(self.updater.status()['state'],'handoff_ready')

    def test_handoff_failure_resumes_without_install_or_force_stop(self):
        resumed = []
        self.updater.busy = lambda: []
        self.updater.quiesce = lambda: True
        self.updater.resume = lambda: resumed.append(True)
        self.updater.handoff = lambda request: (_ for _ in ()).throw(OSError('synthetic helper failure'))
        with patch.object(self.updater, 'fetch', side_effect=[self.envelope(), self.package]), \
             patch('knowledge_bridge.full_installer_update.registrations', self.registry):
            self.updater.start()
            self.join()
        self.assertEqual(self.updater.status()['state'], 'failed')
        self.assertEqual(resumed, [True])
        self.assertEqual((self.program / 'VERSION').read_text(), '1.0.0')

    def test_startup_check_is_check_only_and_reminder_defers_it(self):
        self.updater.remind(600)
        with patch.object(self.updater, 'check') as check:
            self.updater.startup_check()
            check.assert_not_called()
        self.updater._save('reminder.json', {'remind_at': 0})
        checked = threading.Event()
        with patch.object(self.updater, 'check', side_effect=lambda: checked.set()), \
             patch.object(self.updater, 'download') as download:
            self.updater.startup_check()
            self.assertTrue(checked.wait(1))
            download.assert_not_called()

    def test_404_is_unavailable_and_never_current(self):
        self.updater.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
        result = self.updater.check()
        self.assertEqual(result['state'], 'unavailable')
        self.assertNotIn('latest', result)

    def test_formal_channel_refuses_placeholders_and_signed_test_artifact(self):
        for value in ['http://updates.example/a', 'https://updates.example/a', 'https://example.com/a', 'https://localhost/a']:
            with self.assertRaises(ValueError):
                public_url(value)
        self.config['test_only'] = False
        (self.program / 'knowledge_bridge/update-channel.json').write_text(json.dumps(self.config), 'utf-8')
        with self.assertRaises(ValueError):
            self.updater.verify(self.envelope())

    def test_full_installer_streams_and_checks_pe_version_hash_without_authenticode_requirement(self):
        body = b'MZ' + b'synthetic-installer' * 40000
        record = {**self.record, 'kind': 'complete-installer', 'installer_name': 'Jiedu-1.1.0-windows-x64-setup.exe',
                  'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
        self.updater._save('channel-envelope.json', json.loads(self.envelope(record)))
        self.updater.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))
        with patch('desktop.update_release.pe_product_version', return_value='1.1.0'):
            self.assertEqual(self.updater.download()['state'], 'downloaded')
            verified = self.updater.root / 'verified-installer.exe'
            self.assertEqual(verified.read_bytes(), body)
            result = inspect_installer(verified, record, version_reader=lambda p: '1.1.0')
            self.assertFalse(result['authenticode_required'])
            with self.assertRaises(ValueError):
                inspect_installer(verified, record, version_reader=lambda p: '1.0.0')
        verified.write_bytes(b'MZtampered')
        with self.assertRaises(ValueError):
            inspect_installer(verified, record, version_reader=lambda p: '1.1.0')

    def test_installation_ownership_rejects_other_target_machine_or_marker(self):
        self.assertEqual(installation_owner(self.program, registry_reader=self.registry)['target'], str(self.target))
        for rows in [[], [{'scope': 'machine', 'location': str(self.target)}], [{'scope': 'user', 'location': str(self.root / 'other')}]]:
            with self.assertRaises(ValueError):
                installation_owner(self.program, registry_reader=lambda: rows)
        (self.target / 'jiedu-install.txt').write_text('wrong')
        with self.assertRaises(ValueError):
            installation_owner(self.program, registry_reader=self.registry)

    def test_handoff_rechecks_signed_bytes_and_target_not_arbitrary_command(self):
        self.updater._save('channel-envelope.json', json.loads(self.envelope()))
        artifact = self.updater.root / 'verified.jiedu'
        artifact.write_bytes(self.package)
        request = prepare_handoff(self.program, self.updater.root, self.record, artifact, registry_reader=self.registry)
        self.assertEqual(request['format'], HANDOFF_FORMAT)
        self.assertNotIn('command', request)
        checked = validate_handoff(request['request_path'], self.program, registry_reader=self.registry)
        self.assertEqual(checked['sha256'], self.record['sha256'])
        artifact.write_bytes(b'changed')
        with self.assertRaises(Exception):
            validate_handoff(request['request_path'], self.program, registry_reader=self.registry)

    def test_two_instances_cannot_replace_running_workflow_or_download_twice(self):
        other = OnlineUpdate(self.program, self.root / 'state')
        self.updater.busy = lambda: ['synthetic active task']
        with patch.object(self.updater, 'fetch', side_effect=[self.envelope(), self.package]), \
             patch.object(other, 'fetch') as second_fetch:
            first = self.updater.start()
            self.wait_state('waiting_idle')
            second = other.start()
            self.assertEqual(first['job_id'], second['job_id'])
            second_fetch.assert_not_called()
            self.updater.cancel()
            self.join()

    def helper_fixture(self, state='complete', *, backup=False):
        token='a'*32
        work=self.target.parent/('.jiedu-update-'+token)
        work.mkdir()
        prior=self.target.with_name(self.target.name+'.previous-'+token)
        if backup:
            shutil.copytree(self.target, prior)
        job={'format':HANDOFF_FORMAT,'token':token,'target':str(self.target),'backup':str(prior),
             'kind':'complete-installer','old_version':'1.0.0',
             'old_native_sha256':hashlib.sha256((self.target/'界渡.exe').read_bytes()).hexdigest()}
        (work/'job.json').write_text(json.dumps(job),'utf-8')
        (work/'helper-identity.txt').write_text(HANDOFF_FORMAT,'ascii')
        outcome={'state':state,'token':token}
        if backup:
            baseline=work/'backup-files.json';baseline.write_text(json.dumps({'format':'jiedu-update-tree-v1','files':tree_inventory(prior)}),'utf-8')
            outcome['backup_inventory_sha256']=hashlib.sha256(baseline.read_bytes()).hexdigest()
        baseline=work/'helper-files.json';baseline.write_text(json.dumps({'format':'jiedu-update-tree-v1','files':tree_inventory(work,excluded={'result.json','helper-files.json'})}),'utf-8')
        outcome['helper_inventory_sha256']=hashlib.sha256(baseline.read_bytes()).hexdigest()
        (work/'result.json').write_text(json.dumps(outcome),'utf-8')
        self.updater._save('helper.json',{'work':str(work),'token':token,'helper_pid':12345})
        return work,prior

    def test_uninstaller_entries_require_exact_000_names_and_regular_files(self):
        control=self.root/'unins-control'
        shutil.copytree(self.target,control)
        for name in ('unins000.exe','unins000.dat','unins000.msg'):
            (control/name).write_bytes(b'synthetic known Inno file, never executed')
        owner=installation_owner(control/'.jiedu/program',registry_reader=lambda:[{'scope':'user','location':str(control)}])
        self.assertEqual(owner['target'],str(control))
        for index,(name,is_directory) in enumerate((('unins999.exe',False),('unins999.exe',True),
                                                  ('unins000.exe',True),('unins000.dat',True),
                                                  ('unins000.msg',True))):
            with self.subTest(name=name,is_directory=is_directory):
                target=self.root/('unins-reject-'+str(index))
                shutil.copytree(self.target,target)
                entry=target/name
                if is_directory:
                    entry.mkdir();(entry/'note.txt').write_text('synthetic user note')
                else:
                    entry.write_bytes(b'synthetic user file')
                with self.assertRaises(ValueError):
                    installation_owner(target/'.jiedu/program',registry_reader=lambda:[{'scope':'user','location':str(target)}])
                self.assertTrue(entry.exists())

    @unittest.skipUnless(os.name=='nt','Real Windows PowerShell atomic-file semantics')
    def test_real_report_function_replaces_json_across_states_on_powershell7_and_fallback(self):
        preferred=Path(os.environ.get('LOCALAPPDATA',''))/'Microsoft/WindowsApps/pwsh.exe'
        ps7=str(preferred) if preferred.is_file() else shutil.which('pwsh')
        if not ps7:self.skipTest('PowerShell 7 is unavailable')
        hosts=[('ps7',ps7,7)]
        legacy=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
        if legacy.is_file():hosts.append(('ps51-compatibility',str(legacy),5))
        prefix=HELPER_SCRIPT.split("\ntry {\n  if($j.format",1)[0]
        body=r'''
$states=@('waiting_exit','applying','installed','finalizing','complete')
foreach($state in $states) {
  Report $state ('synthetic '+$state)
  $read=Get-Content -LiteralPath (Join-Path $work 'result.json') -Raw -Encoding UTF8|ConvertFrom-Json
  if($read.state -ne $state -or $read.phase -ne $state -or $read.token -ne $j.token){throw 'Atomic report did not contain the current complete JSON state'}
}
@{major=$PSVersionTable.PSVersion.Major;version=[string]$PSVersionTable.PSVersion;transitions=$states.Count;final=$read.state}|ConvertTo-Json -Compress
'''
        for label,executable,minimum in hosts:
            with self.subTest(host=label):
                work=self.root/('report-'+label);work.mkdir()
                job={'work':str(work),'target':str(self.target),'backup':str(work/'previous'),
                     'failed':str(work/'failed'),'version':'1.1.0','token':'e'*32,'helper_host':executable}
                job_path=work/'job.json';job_path.write_text(json.dumps(job),'utf-8')
                script=work/'report-functions.ps1';script.write_text(prefix+body,'utf-8-sig')
                result=subprocess.run([executable,'-NoLogo','-NoProfile','-NonInteractive','-File',str(script),
                    '-JobFile',str(job_path)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
                self.assertEqual(result.returncode,0,result.stderr[-3000:])
                proof=json.loads(result.stdout)
                self.assertGreaterEqual(proof['major'],minimum)
                self.assertEqual(proof['transitions'],5)
                self.assertEqual(proof['final'],'complete')

    @unittest.skipUnless(os.name=='nt','Real Windows PowerShell argument-array semantics')
    def test_real_installer_argument_array_preserves_quoted_paths_on_powershell7_and_fallback(self):
        preferred=Path(os.environ.get('LOCALAPPDATA',''))/'Microsoft/WindowsApps/pwsh.exe'
        ps7=str(preferred) if preferred.is_file() else shutil.which('pwsh')
        if not ps7:self.skipTest('PowerShell 7 is unavailable')
        hosts=[('ps7',ps7,7)]
        legacy=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
        if legacy.is_file():hosts.append(('ps51-compatibility',str(legacy),5))
        # Execute only the actual array assignment, never helper main or installer.
        assignment=next(line for line in HELPER_SCRIPT.splitlines() if line.strip().startswith('$arguments='))
        prefix=r'''param([string]$JobFile)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$j=Get-Content -LiteralPath $JobFile -Raw -Encoding UTF8|ConvertFrom-Json
$target=$j.target
$work=$j.work
'''
        suffix=r'''
@{major=$PSVersionTable.PSVersion.Major;version=[string]$PSVersionTable.PSVersion;count=$arguments.Count;arguments=@($arguments)}|ConvertTo-Json -Compress
'''
        for label,executable,minimum in hosts:
            with self.subTest(host=label):
                work=self.root/('arguments-'+label);work.mkdir()
                target=self.root/'中文 安装目录'
                helper_work=work/'中文 助手目录'
                job_path=work/'job.json'
                job_path.write_text(json.dumps({'target':str(target),'work':str(helper_work)},ensure_ascii=False),'utf-8')
                script=work/'arguments-functions.ps1'
                script.write_text(prefix+assignment+'\n'+suffix,'utf-8-sig')
                result=subprocess.run([executable,'-NoLogo','-NoProfile','-NonInteractive','-File',str(script),
                    '-JobFile',str(job_path)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
                self.assertEqual(result.returncode,0,result.stderr[-3000:])
                proof=json.loads(result.stdout)
                self.assertGreaterEqual(proof['major'],minimum)
                self.assertEqual(proof['count'],6)
                self.assertEqual(proof['arguments'],['/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-',
                    '/DIR="'+str(target)+'"','/LOG="'+str(helper_work/'installer.log')+'"'])

    def test_workspace_inside_install_or_unknown_top_level_is_rejected_without_moving(self):
        workspace=self.target/'.jiedu/private-vault'
        workspace.mkdir()
        evidence=workspace/'synthetic.md';evidence.write_text('synthetic original')
        with self.assertRaisesRegex(ValueError,'工作区位于安装目录内'):
            check_workspace_boundary(self.target,workspace)
        check_workspace_boundary(self.target,self.program/'knowledge-space')  # absent first-run fallback
        with self.assertRaises(ValueError):
            check_workspace_boundary(self.target,self.program/'knowledge-space',selected=True)
        extra=self.target/'user-notes.md';extra.write_text('synthetic original')
        with self.assertRaisesRegex(ValueError,'额外文件'):
            installation_owner(self.program,registry_reader=self.registry)
        self.assertEqual(evidence.read_text(),'synthetic original')
        self.assertEqual(extra.read_text(),'synthetic original')

    def test_self_contained_layout_is_explicitly_not_automatic_full_install(self):
        (self.program.parent/'package.json').write_text(json.dumps({'format_version':1,'layout':'jiedu-self-contained-v1','program':'program','workspace':'..'}))
        (self.target/'jiedu-install.txt').unlink()
        with self.assertRaisesRegex(ValueError,'自包含工作区.*资料无需搬移'):
            installation_owner(self.program,registry_reader=self.registry)

    def test_retention_only_plans_attributed_successful_backup_and_preserves_failed_unknown(self):
        work,prior=self.helper_fixture(backup=True)
        rows=retention_plan(self.target)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['backup'],str(prior))
        self.assertTrue(prior.exists())  # preflight never deletes
        outcome=json.loads((work/'result.json').read_text());outcome['state']='recovery_required'
        (work/'result.json').write_text(json.dumps(outcome))
        with self.assertRaisesRegex(ValueError,'失败的更新恢复材料'):
            retention_plan(self.target)
        self.assertTrue(prior.exists())
        outcome['state']='complete';(work/'result.json').write_text(json.dumps(outcome))
        (self.target.with_name(self.target.name+'.previous-'+'b'*32)).mkdir()
        with self.assertRaisesRegex(ValueError,'未归属'):
            retention_plan(self.target)

    def test_retention_accepts_mutable_current_log_and_legacy_inventory(self):
        work,prior=self.helper_fixture(backup=True)
        log=work/'helper.log';log.write_text('live helper output','utf-8')
        self.assertEqual(len(retention_plan(self.target)),1)
        log.write_text('more live helper output','utf-8')
        self.assertEqual(len(retention_plan(self.target)),1)
        baseline=work/'helper-files.json'
        baseline.write_text(json.dumps({'format':'jiedu-update-tree-v1',
            'files':tree_inventory(work,excluded={'result.json','helper-files.json'})}),'utf-8')
        outcome=json.loads((work/'result.json').read_text('utf-8'))
        outcome['helper_inventory_sha256']=hashlib.sha256(baseline.read_bytes()).hexdigest()
        (work/'result.json').write_text(json.dumps(outcome),'utf-8')
        self.assertEqual(len(retention_plan(self.target)),1)
        log.write_text('changed older inventory output','utf-8')
        with self.assertRaisesRegex(ValueError,'助手材料后来新增或变化'):
            retention_plan(self.target)

    def test_helper_lifetime_blocks_second_start_after_backend_worker_is_gone(self):
        self.helper_fixture(state='applying')
        restarted=OnlineUpdate(self.program,self.root/'state')
        with patch.object(restarted,'fetch') as network:
            state=restarted.start()
            self.assertEqual(state['state'],'applying')
            self.assertTrue(state['running'])
            network.assert_not_called()

    def test_terminal_helper_does_not_hide_next_release_after_service_restart(self):
        self.helper_fixture(state='complete')
        self.updater._save('workflow.json',{'state':'restarting','token':'a'*32})
        self.updater._save('last-check.json',{'state':'current','latest':'1.0.0'})
        self.updater.remind(600)
        restarted=OnlineUpdate(self.program,self.root/'state')
        with patch.object(restarted,'fetch',return_value=self.envelope()):
            self.assertEqual(restarted.check()['state'],'available')
        self.assertEqual(restarted.status()['state'],'available')
        self.assertIsNone(restarted.status()['remind_at'])

    def test_stable_channel_rejects_signed_prerelease_without_guessing_it_is_current(self):
        self.config['channel']='stable'
        (self.program/'knowledge_bridge/update-channel.json').write_text(json.dumps(self.config))
        with self.assertRaises(ValueError):
            self.updater.verify(self.envelope({**self.record,'version':'1.2.0-rc.1'}))

    def test_recover_is_explicit_and_waits_idle_before_handoff(self):
        self.helper_fixture(state='recovery_required',backup=True)
        self.updater.busy=lambda:[]
        self.updater.quiesce=lambda:True
        with self.assertRaisesRegex(ValueError,'先恢复'):
            self.updater.start()
        with patch('knowledge_bridge.full_installer_update.start_recovery') as recover:
            self.updater.recover()
            self.join()
            recover.assert_called_once_with(self.program,self.updater.root)

    def test_long_running_scheduler_checks_again_only_when_due_and_closes(self):
        event=threading.Event()
        calls=[]
        def fetch(url,limit):
            calls.append(url);event.set();return self.envelope()
        with patch.object(self.updater,'fetch',side_effect=fetch):
            self.updater.startup_check()
            self.assertTrue(event.wait(1))
            until=time.monotonic()+1
            while self.updater._read('last-attempt.json').get('failures')!=0 and time.monotonic()<until:
                time.sleep(.005)
            self.updater.startup_check()
            time.sleep(.03)
            self.assertEqual(len(calls),1)
            event.clear()
            self.updater._save('last-attempt.json',{'next_check_at':time.time()-1,'failures':0})
            self.updater.startup_check()
            self.assertTrue(event.wait(1))
            self.updater.close()
            self.assertFalse(self.updater.check_worker.is_alive())
        self.assertEqual(len(calls),2)

    @unittest.skipUnless(os.name=='nt','Windows helper filesystem semantics')
    def test_actual_helper_restore_and_success_retention_functions_on_synthetic_trees(self):
        host=Path(os.environ.get('LOCALAPPDATA',''))/'Microsoft/WindowsApps/pwsh.exe'
        executable=str(host) if host.is_file() else shutil.which('pwsh')
        if not executable:self.skipTest('PowerShell 7 is unavailable')
        # Invoke only reviewed function definitions, replacing process/native
        # operations; no main installer code, registry calls or real executable.
        script=self.root/'helper-functions.ps1'
        prefix=HELPER_SCRIPT.split("\ntry {\n  if($j.format",1)[0]
        token='c'*32
        backup=self.target.with_name(self.target.name+'.previous-'+token)
        failed=self.target.with_name(self.target.name+'.failed-'+token)
        work=self.root/('.jiedu-update-'+token);work.mkdir()
        backup.mkdir();(backup/'.jiedu/control').mkdir(parents=True)
        (backup/'.jiedu/program').mkdir()
        (backup/'.jiedu/program/VERSION').write_text('1.0.0')
        (backup/'jiedu-install.txt').write_text('jiedu-consumer-install-v1')
        (backup/'.jiedu/control/retained.bin').write_bytes(b'exact synthetic private bytes')
        (backup/'界渡.exe').write_bytes(b'old shell')
        job={'work':str(work),'target':str(self.target),'backup':str(backup),'failed':str(failed),
             'local_data':str(self.root/'control'),'token':token,'version':'1.1.0','old_version':'1.0.0',
             'proof':{'launch_id':'synthetic-launch-00001'},'required_acks':[{'role':'console','pid':42}],
             'old_native_sha256':hashlib.sha256(b'old shell').hexdigest(),
             'helper_host':executable,'format':HANDOFF_FORMAT,'install_identity':'jiedu-consumer-install-v1','cleanup':[]}
        job_path=self.root/'helper-job.json';job_path.write_text(json.dumps(job),'utf-8')
        checks=r'''
function TargetProcesses { return @() }
function Start-Process { param($FilePath,$WorkingDirectory,$WindowStyle) }
function Get-CimInstance { param($ClassName) return @() }
$registry=@()
$script:restoreTry=0
function RestoreRegistry {$script:restoreTry++;if($script:restoreTry -eq 1){throw 'synthetic post-restore registry failure'}}
$ackDirectory=Join-Path $j.local_data 'runtime';[IO.Directory]::CreateDirectory($ackDirectory)|Out-Null
$rejected=$false;try{NormalAcks}catch{$rejected=$true};if(-not $rejected){throw 'Missing normal ack was accepted'}
@{token=$j.token;launch_id=$j.proof.launch_id;role='console';pid=42;complete=$true}|ConvertTo-Json|Set-Content -LiteralPath (Join-Path $ackDirectory ('update-stop-'+$j.token+'-console.json'))
NormalAcks
Preserve (Join-Path $backup '.jiedu\control') (Join-Path $target '.jiedu\control')
try{RestorePrevious}catch{if($_.Exception.Message -ne 'synthetic post-restore registry failure'){throw}}
RestorePrevious
if([IO.File]::ReadAllText((Join-Path $target '.jiedu\control\retained.bin')) -ne 'exact synthetic private bytes'){throw 'Private bytes were not restored'}
if(-not (Test-Path -LiteralPath $failed)){throw 'Failed tree was discarded'}
if(Test-Path -LiteralPath $backup){throw 'Previous directory not restored'}
# A successful older transaction may be retired only after another backup exists.
[IO.Directory]::CreateDirectory($backup)|Out-Null
[IO.File]::WriteAllText((Join-Path $backup 'jiedu-install.txt'),$j.install_identity)
[IO.File]::WriteAllText((Join-Path $backup '界渡.exe'),'old shell')
$oldToken='dddddddddddddddddddddddddddddddd'
$oldBackup=$target+'.previous-'+$oldToken
$oldWork=Join-Path (Split-Path -Parent $target) ('.jiedu-update-'+$oldToken)
[IO.Directory]::CreateDirectory($oldBackup)|Out-Null
[IO.Directory]::CreateDirectory($oldWork)|Out-Null
[IO.File]::WriteAllText((Join-Path $oldBackup 'jiedu-install.txt'),$j.install_identity)
[IO.File]::WriteAllText((Join-Path $oldBackup '界渡.exe'),'old synthetic shell')
$oldHash=(Get-FileHash -LiteralPath (Join-Path $oldBackup '界渡.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
@{target=$target;token=$oldToken}|ConvertTo-Json|Set-Content -LiteralPath (Join-Path $oldWork 'job.json')
[IO.File]::WriteAllText((Join-Path $oldWork 'helper-identity.txt'),$j.format)
WriteTree $oldBackup (Join-Path $oldWork 'backup-files.json')
WriteTree $oldWork (Join-Path $oldWork 'helper-files.json') @('result.json','helper-files.json')
@{state='complete';backup_inventory_sha256=(Get-FileHash -LiteralPath (Join-Path $oldWork 'backup-files.json') -Algorithm SHA256).Hash.ToLowerInvariant();helper_inventory_sha256=(Get-FileHash -LiteralPath (Join-Path $oldWork 'helper-files.json') -Algorithm SHA256).Hash.ToLowerInvariant()}|ConvertTo-Json|Set-Content -LiteralPath (Join-Path $oldWork 'result.json')
$j.cleanup=@(@{target=$target;token=$oldToken;work=$oldWork;backup=$oldBackup;old_native_sha256=$oldHash})
RetireSuccessful
if((Test-Path -LiteralPath $oldBackup) -or (Test-Path -LiteralPath $oldWork)){throw 'Attributed old success not retired'}
if(-not (Test-Path -LiteralPath $failed)){throw 'Failed recovery material was removed'}
if(-not (Test-Path -LiteralPath $backup)){throw 'Newest rollback was removed'}
Write-Output 'synthetic_restore_and_retention_passed'
'''
        script.write_text(prefix+checks,'utf-8-sig')
        result=subprocess.run([executable,'-NoLogo','-NoProfile','-NonInteractive','-File',str(script),'-JobFile',str(job_path)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
        self.assertEqual(result.returncode,0,result.stderr[-3000:])
        self.assertIn('synthetic_restore_and_retention_passed',result.stdout)

    def test_install_inventory_binds_icon_program_runtime_and_rejects_unknown_additions(self):
        runtime=self.program.parent/'runtime/python';runtime.mkdir(parents=True)
        (runtime/'python.exe').write_bytes(b'synthetic interpreter')
        (self.target/'jiedu-v1.ico').write_bytes(b'synthetic icon')
        self.assertEqual(installation_owner(self.program,registry_reader=self.registry)['target'],str(self.target))
        with self.assertRaisesRegex(ValueError,'缺少受审文件清单'):
            verify_installation_inventory(self.program)
        manifest=self.program.parent/'install-files.json'
        manifest.write_text(json.dumps({'format':INVENTORY_FORMAT,'files':tree_inventory(self.target)}),'utf-8')
        self.assertGreater(verify_installation_inventory(self.program)['files'],3)
        extra=runtime/'unregistered.bin';extra.write_bytes(b'user added synthetic data')
        with self.assertRaisesRegex(ValueError,'未登记'):
            verify_installation_inventory(self.program)
        self.assertEqual(extra.read_bytes(),b'user added synthetic data')

    def test_generated_cache_and_optional_runtime_survive_inventory_preflight(self):
        import py_compile
        module=self.program/'knowledge_bridge/example.py'
        module.write_text('value = 1\n','utf-8')
        runtime=self.program.parent/'runtime/python';runtime.mkdir(parents=True)
        (runtime/'python.exe').write_bytes(b'synthetic interpreter')
        manifest=self.program.parent/'install-files.json'
        manifest.write_text(json.dumps({'format':INVENTORY_FORMAT,'files':tree_inventory(self.target)}),'utf-8')
        py_compile.compile(str(module),doraise=True)
        optional=self.program.parent/'runtime/ocr/custom-language.bin'
        optional.parent.mkdir();optional.write_bytes(b'optional component retained')
        result=verify_installation_inventory(self.program)
        self.assertEqual(result['generated_caches'],1)
        self.assertEqual(result['preserved_optional_runtime'],['ocr'])
        extra=module.parent/'__pycache__/my-notes.md';extra.write_text('synthetic user note')
        with self.assertRaisesRegex(ValueError,'未登记'):
            verify_installation_inventory(self.program)
        self.assertEqual(optional.read_bytes(),b'optional component retained')

    def test_later_backup_or_helper_additions_are_never_eligible_for_cleanup(self):
        work,prior=self.helper_fixture(backup=True)
        (prior/'later-notes.md').write_text('synthetic later data')
        with self.assertRaisesRegex(ValueError,'新增或修改'):
            retention_plan(self.target)
        self.assertTrue((prior/'later-notes.md').exists())

    def test_partial_helper_report_blocks_new_update_instead_of_disappearing(self):
        work,prior=self.helper_fixture(state='applying')
        (work/'result.json').write_text('{')
        restarted=OnlineUpdate(self.program,self.root/'state')
        with patch.object(restarted,'fetch') as network:
            self.assertTrue(restarted.start()['running'])
            network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
