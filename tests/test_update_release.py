"""Release fixtures use temporary synthetic keys and never contact a server."""
import base64
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from desktop import build_signed_update as builder
from desktop import update_release as release
from desktop import release_version


class ReleaseToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='release-tools-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = Ed25519PrivateKey.generate()
        self.key_path = self.root / 'private' / 'fixture.pem'
        self.key_path.parent.mkdir()
        self.key_path.write_bytes(self.key.private_bytes(serialization.Encoding.PEM,
                                                       serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))
        self.previous = self.program('previous', '2.0.0')
        self.source = self.program('source', '2.0.1')
        # Syntax-only synthetic HTTPS addresses. No DNS lookup or connection is made.
        self.url = 'https://updates.example/releases/2.0.1.jiedu'
        self.config = {'format': release.FORMAT, 'channel': 'preview',
                       'test_only': True,
                       'manifest_url': 'https://updates.example/channel.json',
                       'asset_hosts': ['updates.example'],
                       'program_asset_url': self.url,
                       'installer_asset_url': 'https://updates.example/setup.exe'}
        self.runtime = self.root / 'runtime.json'
        self.runtime.write_text(json.dumps([{'path': 'python.exe', 'sha256': 'a' * 64,
                                            'bytes': 123}]), 'utf-8')
        self.shell = self.root / 'current-shell.exe'
        self.previous_shell = self.root / 'previous-shell.exe'
        for path in [self.shell, self.previous_shell]:
            path.write_bytes(b'MZ synthetic shell bytes, never executed')

    def program(self, name, version):
        root = self.root / name
        contents = {'VERSION': version,
                    'pyproject.toml': '[project]\nversion="' + version + '"\n',
                    'requirements.lock.txt': 'synthetic-dependency==1\n',
                    'knowledge_bridge/__init__.py': '__version__ = "' + version + '"\n',
                    'knowledge_bridge/update-public-key.txt': base64.b64encode(
                        self.key.public_key().public_bytes_raw()).decode('ascii'),
                    'static/app.js': '/* synthetic ' + version + ' */',
                    'desktop/Cargo.toml': '[package]\nversion="1.0.0"\n',
                    'desktop/Cargo.lock': '[[package]]\nname = "jiedu-desktop"\nversion = "1.0.0"\n',
                    'desktop/tauri.conf.json': '{"version":"1.0.0"}', 'desktop/build.rs': '// build',
                    'desktop/build_native.py': '# synthetic native build script',
                    'desktop/icon_assets.py': '# synthetic icon build input',
                    'desktop/installer.iss': ('; \\.jiedu\\program\\VERSION\nAppVersion={#ReleaseVersion}\n'
                                              'VersionInfoVersion={#ReleaseFileVersion}\n'
                                              'VersionInfoProductVersion={#ReleaseFileVersion}\n'
                                              'VersionInfoProductTextVersion={#ReleaseVersion}\n'
                                              'OutputBaseFilename=Jiedu-{#ReleaseVersion}-windows-x64-setup\n'),
                    'desktop/src/main.rs': '// shell', 'desktop/ui/index.html': '<html></html>',
                    'desktop/icons/icon.ico': 'synthetic icon'}
        for path, body in contents.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, 'utf-8')
        (root / 'public-source-files.txt').write_text(
            '\n'.join(sorted([*contents, 'public-source-files.txt'])) + '\n', 'utf-8')
        return root

    def build(self, **kwargs):
        params = dict(runtime_manifest=self.runtime, previous_runtime_manifest=self.runtime,
                      test_only=True, native_exe=self.shell, previous_native_exe=self.previous_shell)
        params.update(kwargs)
        return builder.build(self.source, self.previous, self.key_path, self.root / 'output',
                             self.url, self.config['asset_hosts'], **params)

    def test_icon_assets_change_requires_full_installer_even_with_identical_native_exe(self):
        self.assertEqual(self.shell.read_bytes(),self.previous_shell.read_bytes())
        (self.source/'desktop/icon_assets.py').write_text('# changed synthetic native icon input','utf-8')
        with self.assertRaisesRegex(ValueError,'Native build inputs changed.*full installer'):
            self.build()
        self.assertFalse((self.root/'output').exists())

    def test_signed_full_installer_channel_uses_expected_key_hash_version_and_no_execution(self):
        installer=self.root/'Jiedu-2.0.1-windows-x64-setup.exe'
        installer.write_bytes(b'MZ synthetic installer never executed')
        with patch.object(release,'pe_product_version',return_value='2.0.1'), \
             patch.object(release,'authenticode',return_value={'status':'NotSigned'}):
            envelope=builder.build_installer(self.source,installer,self.key_path,self.root/'full-output',self.config,test_only=True)
            report=release.verify_installer(self.config,self.previous,installer,envelope,test_only=True)
        self.assertTrue(report['offline_checks_passed'])
        self.assertFalse(report['authenticode_required'])
        self.assertEqual(report['kind'],'complete-installer')
        installer.write_bytes(b'MZ changed')
        with self.assertRaises(ValueError):
            release.verify_installer(self.config,self.previous,installer,envelope,test_only=True)

    def resign(self, path, **changes):
        envelope = json.loads(path.read_text('utf-8'))
        record = json.loads(base64.b64decode(envelope['payload']))
        record.update(changes)
        payload = json.dumps(record).encode('utf-8')
        path.write_text(json.dumps({'payload': base64.b64encode(payload).decode('ascii'),
                                   'signature': base64.b64encode(self.key.sign(payload)).decode('ascii')}),
                        'utf-8')

    def test_real_builder_and_offline_verifier_use_only_allowlisted_program_files(self):
        hidden = self.source / 'knowledge_bridge/unreviewed.txt'
        hidden.write_text('DO_NOT_PACKAGE', 'utf-8')
        (self.source / 'config.json').write_text('DO_NOT_PACKAGE', 'utf-8')
        package = self.build()
        with zipfile.ZipFile(package) as archive:
            names = archive.namelist()
            self.assertNotIn('program/knowledge_bridge/unreviewed.txt', names)
            self.assertNotIn('program/config.json', names)
            self.assertFalse(any(n.startswith('program/desktop/') for n in names))
            self.assertIn('program/VERSION', names)
            self.assertNotIn('program/knowledge_bridge/update-public-key.txt', names)
        with patch('httpx.Client', side_effect=AssertionError('network forbidden')):
            result = release.verify_program(self.config, self.previous, package,
                                            package.parent / 'channel.json', test_only=True)
        self.assertTrue(result['offline_checks_passed'])
        self.assertFalse(result['public_channel_live'])
        self.assertTrue(result['owner_address_review_required'])
        self.assertEqual((self.previous / 'VERSION').read_text(), '2.0.0')

    def test_placeholder_local_credential_and_non_https_addresses_rejected(self):
        urls = ['', 'http://updates.example/x', 'https://example.com/x',
                'https://cdn.example.net/x', 'https://updates.test/x', 'https://updates.invalid/x',
                'https://localhost/x', 'https://127.0.0.1/x',
                'https://user:secret@updates.example/x',
                'https://updates.example/{repo}/x',
                'https://updates.example/your-repo/x']
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                release.public_release_url(url)
        with self.assertRaises(ValueError):
            release.validate_config(release.config_template())

    def test_unpinned_asset_and_duplicate_hosts_rejected(self):
        with self.assertRaisesRegex(ValueError, 'pinned'):
            release.validate_config({**self.config, 'asset_hosts': ['another.example']}, test_only=True)
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            release.validate_hosts(['updates.example', 'UPDATES.EXAMPLE'], test_only=True)

    def test_runtime_change_requires_full_installer_even_when_lock_unchanged(self):
        changed = self.root / 'changed-runtime.json'
        changed.write_text(json.dumps([{'path': 'python.exe', 'sha256': 'b' * 64, 'bytes': 123}]))
        with self.assertRaisesRegex(ValueError, 'runtime changed'):
            self.build(runtime_manifest=changed)
        self.assertFalse((self.root / 'output').exists())

    def test_missing_runtime_inventory_and_changed_native_require_full_installer(self):
        with self.assertRaisesRegex(ValueError, 'inventories are required'):
            self.build(runtime_manifest=None)
        (self.source / 'desktop/src/main.rs').write_text('// changed shell')
        with self.assertRaisesRegex(ValueError, 'Native build inputs changed'):
            self.build()

    def test_program_scope_sync_build_verify_then_real_native_change_is_rejected(self):
        (self.source / 'VERSION').write_text('2.0.1-rc.1\n', 'utf-8')
        (self.source / 'pyproject.toml').write_text('[project]\nversion="2.0.0"\n', 'utf-8')
        (self.source / 'knowledge_bridge/__init__.py').write_text('__version__ = "2.0.0"\n', 'utf-8')
        native_before = release.native_inventory(self.source)
        synced = release_version.sync(self.source, scope='program')
        self.assertTrue(synced['passed'])
        self.assertEqual(release.program_version(self.source), '2.0.1-rc.1')
        self.assertEqual(release.native_inventory(self.source), native_before)
        with self.assertRaises(ValueError):
            release_version.check(self.source, scope='full')
        package = self.build()
        verified = release.verify_program(self.config, self.previous, package,
                                          package.parent / 'channel.json', test_only=True)
        self.assertTrue(verified['offline_checks_passed'])
        self.assertEqual(verified['version'], '2.0.1-rc.1')
        (self.source / 'desktop/src/main.rs').write_text('// genuine native change', 'utf-8')
        with self.assertRaisesRegex(ValueError, 'Native build inputs changed'):
            self.build()

    def test_allowlist_escape_alias_or_missing_member_is_rejected(self):
        path = self.source / 'public-source-files.txt'
        original = path.read_text('utf-8')
        for added in ['../outside.txt', 'static/app.js', 'static/APP.js', 'static/missing.js']:
            path.write_text(original + added + '\n', 'utf-8')
            with self.subTest(added=added), self.assertRaises(ValueError):
                self.build()
        path.write_text(original, 'utf-8')

    def test_private_key_and_output_must_be_outside_source(self):
        with self.assertRaisesRegex(ValueError, 'outside source'):
            builder.build(self.source, self.previous, self.source / 'private.pem',
                          self.root / 'out', self.url, self.config['asset_hosts'])
        with self.assertRaisesRegex(ValueError, 'outside source'):
            builder.build(self.source, self.previous, self.key_path,
                          self.source / 'out', self.url, self.config['asset_hosts'])

    def test_wrong_signing_key_and_version_mismatch_rejected(self):
        wrong = Ed25519PrivateKey.generate()
        self.key_path.write_bytes(wrong.private_bytes(serialization.Encoding.PEM,
                                                     serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()))
        with self.assertRaisesRegex(ValueError, 'installed trust key'):
            self.build()
        (self.source / 'VERSION').write_text('9.0.0')
        with self.assertRaisesRegex(ValueError, 'VERSION'):
            self.build()

    def test_gzip_runtime_inventory_is_equivalent_and_case_alias_is_rejected(self):
        compressed = self.root / 'runtime.json.gz'
        with gzip.open(compressed, 'wb') as stream:
            stream.write(self.runtime.read_bytes())
        self.assertEqual(release.runtime_inventory(compressed), release.runtime_inventory(self.runtime))
        rows = json.loads(self.runtime.read_text())
        rows.append({**rows[0], 'path': 'PYTHON.exe'})
        self.runtime.write_text(json.dumps(rows))
        with self.assertRaisesRegex(ValueError, 'aliased'):
            release.runtime_inventory(self.runtime)

    def test_unsigned_or_bad_channel_is_rejected(self):
        package = self.build()
        envelope = package.parent / 'channel.json'
        original = envelope.read_text('utf-8')
        for body in [{}, {'payload': base64.b64encode(b'{}').decode(), 'signature': ''}]:
            envelope.write_text(json.dumps(body), 'utf-8')
            with self.assertRaisesRegex(ValueError, '校验失败'):
                release.verify_program(self.config, self.previous, package, envelope, test_only=True)
        envelope.write_text(original, 'utf-8')
        self.resign(envelope, expires_at='2000-01-01T00:00:00Z')
        with self.assertRaises(ValueError):
            release.verify_program(self.config, self.previous, package, envelope, test_only=True)

    def test_signed_version_hash_or_exact_asset_mismatch_rejected(self):
        package = self.build()
        envelope = package.parent / 'channel.json'
        original = envelope.read_text('utf-8')
        for changes, message in [({'version': '2.0.2'}, 'version'),
                                 ({'sha256': 'a' * 64}, 'SHA256'),
                                 ({'url': self.url + '?other=1'}, 'URL')]:
            envelope.write_text(original, 'utf-8')
            self.resign(envelope, **changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                release.verify_program(self.config, self.previous, package, envelope, test_only=True)

    def test_unsigned_program_package_rejected_even_with_valid_channel_signature(self):
        package = self.build()
        unsigned = package.parent / 'unsigned.jiedu'
        with zipfile.ZipFile(package) as original, zipfile.ZipFile(unsigned, 'w') as output:
            for name in original.namelist():
                if name != 'signature.txt':
                    output.writestr(name, original.read(name))
        envelope = package.parent / 'channel.json'
        self.resign(envelope, sha256=release.file_digest(unsigned), size=unsigned.stat().st_size)
        with self.assertRaisesRegex(ValueError, 'signature/member'):
            release.verify_program(self.config, self.previous, unsigned, envelope, test_only=True)

    def installer(self):
        path = self.root / 'Jiedu-2.0.1-windows-x64-setup.exe'
        path.write_bytes(b'MZ synthetic fixture, not an executable installer')
        return path, release.file_digest(path)

    def test_unsigned_manual_preview_installer_keeps_warning_without_forcing_certificate(self):
        path, digest = self.installer()
        with patch.object(release, 'pe_product_version', return_value='2.0.1'), \
                patch.object(release, 'authenticode', return_value={'status': 'NotSigned', 'signer_thumbprint': None}):
            result = release.installer_checklist(path, '2.0.1', digest, config=self.config, test_only=True)
        self.assertTrue(result['candidate_integrity_checks_passed'])
        self.assertTrue(result['offline_checks_passed'])
        self.assertFalse(result['formal_preflight_passed'])
        self.assertFalse(result['authenticode_required'])
        self.assertIn('Authenticode', result['warnings'][0])
        self.assertFalse(result['public_channel_live'])

    def test_installer_wrong_hash_and_pe_version_rejected(self):
        path, digest = self.installer()
        with patch.object(release, 'pe_product_version') as version:
            with self.assertRaisesRegex(ValueError, 'SHA256'):
                release.installer_checklist(path, '2.0.1', 'a' * 64)
            version.assert_not_called()
        with patch.object(release, 'pe_product_version', return_value='2.0.0'):
            with self.assertRaisesRegex(ValueError, 'ProductVersion'):
                release.installer_checklist(path, '2.0.1', digest)

    def test_installer_config_still_required_and_mocked_trusted_signature_only_silences_warning(self):
        path, digest = self.installer()
        # This unit fixture does not create a certificate or sign a real executable.
        with patch.object(release, 'pe_product_version', return_value='2.0.1'), \
                patch.object(release, 'authenticode', return_value={'status': 'Valid', 'signer_thumbprint': 'synthetic-unit-test'}):
            self.assertFalse(release.installer_checklist(path, '2.0.1', digest)['formal_preflight_passed'])
            result = release.installer_checklist(path, '2.0.1', digest, config=self.config, test_only=True)
        self.assertTrue(result['offline_checks_passed'])
        self.assertFalse(result['formal_preflight_passed'])
        self.assertEqual(result['warnings'], [])
        self.assertFalse(result['published'])
        self.assertFalse(result['public_channel_live'])

    def test_cli_unsigned_installer_writes_warning_report_for_manual_preview(self):
        path, digest = self.installer()
        report = self.root / 'installer-report.json'
        config = self.root / 'config.json'
        config.write_text(json.dumps(self.config), 'utf-8')
        with patch.object(release, 'pe_product_version', return_value='2.0.1'), \
                patch.object(release, 'authenticode', return_value={'status': 'NotSigned'}), \
                redirect_stdout(StringIO()):
            result = release.main(['installer-checklist', '--installer', str(path),
                                   '--version', '2.0.1', '--sha256', digest,
                                   '--config', str(config), '--report', str(report), '--test-only'])
        self.assertEqual(result, 0)
        saved = json.loads(report.read_text())
        self.assertTrue(saved['offline_checks_passed'])
        self.assertFalse(saved['formal_preflight_passed'])
        self.assertTrue(saved['warnings'])

    def test_test_only_requires_marker_and_reserved_or_loopback_https(self):
        with self.assertRaisesRegex(ValueError, 'test_only'):
            release.validate_config(self.config)
        for url in ['https://updates.example/x', 'https://127.0.0.1:8443/x']:
            self.assertEqual(release.public_release_url(url, test_only=True), url)
            with self.assertRaises(ValueError):
                release.public_release_url(url)
        with self.assertRaises(ValueError):
            release.public_release_url('https://github.com/x', test_only=True)

    def test_test_only_cannot_use_the_shipping_trust_key(self):
        key = base64.b64encode(release.trust_key(release.TOOL_SOURCE)).decode('ascii')
        for root in [self.source, self.previous]:
            (root / 'knowledge_bridge/update-public-key.txt').write_text(key, 'ascii')
        with self.assertRaisesRegex(ValueError, 'shipping trust key'):
            self.build()

    def test_native_build_script_change_requires_full_installer(self):
        (self.source / 'desktop/build_native.py').write_text('# actual builder change', 'utf-8')
        with self.assertRaisesRegex(ValueError, 'Native build inputs changed'):
            self.build()

    def test_missing_or_different_native_exe_requires_full_installer(self):
        with self.assertRaisesRegex(ValueError, 'Both native EXE files'):
            self.build(native_exe=None)
        self.shell.write_bytes(b'MZ changed synthetic shell')
        with self.assertRaisesRegex(ValueError, 'Native EXE bytes changed'):
            self.build()

    @unittest.skipUnless(os.name == 'nt', 'Windows signature adapter')
    def test_empty_signature_adapter_result_cannot_be_reported_as_a_success(self):
        path, _ = self.installer()
        with patch.object(release.subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout=b'{"status":""}', stderr=b'')):
            with self.assertRaisesRegex(ValueError, 'not readable'):
                release.authenticode(path)


if __name__ == '__main__':
    unittest.main()
