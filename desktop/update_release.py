"""Prepare and verify releases without uploading or configuring an installation.

Templates have empty addresses. Offline checks do not establish domain ownership
or deployment. Installer Authenticode is an optional reputation signal; reviewed
SHA256/version checks remain required. This tool never executes an installer.
"""
import argparse
import base64
import gzip
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from urllib.parse import unquote, urlsplit

FORMAT = 'jiedu-update-release-config-v1'
TOOL_SOURCE = Path(__file__).resolve().parents[1]
NATIVE_FILES = {'desktop/Cargo.toml', 'desktop/Cargo.lock',
                'desktop/tauri.conf.json', 'desktop/build.rs', 'desktop/build_native.py',
                'desktop/icon_assets.py'}
NATIVE_DIRS = ('desktop/src', 'desktop/ui', 'desktop/icons')
PLACEHOLDER = re.compile(r'(?i)(?:<[^>]*>|\{[^}]*\}|\b(?:todo|changeme|replace[-_]?me|'
                         r'your[-_](?:org|repo|domain|host|name)|placeholder)\b)')


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def inventory_digest(inventory):
    return hashlib.sha256(json.dumps(inventory, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def validate_host(host, *, test_only=False):
    if not isinstance(host, str) or not host or host != host.strip():
        raise ValueError('A nonempty public hostname is required')
    host = host.lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if test_only and address.is_loopback:
            return host
        raise ValueError('Literal IP addresses are not formal public release hostnames')
    if any(c in host for c in '/\\:@?#*') or PLACEHOLDER.search(host):
        raise ValueError('Host must be an explicit domain without placeholders or URL syntax')
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host):
        raise ValueError('Use an explicit ASCII/IDNA public hostname')
    if any(not part or len(part) > 63 or part.startswith('-') or part.endswith('-')
           for part in host.split('.')):
        raise ValueError('Invalid public hostname')
    reserved_test = host == 'localhost' or host.endswith(('.localhost', '.test', '.invalid', '.example'))
    if test_only:
        if not reserved_test:
            raise ValueError('Test-only release requires a reserved test hostname or loopback HTTPS')
        return host
    if reserved_test or host.endswith('.local') or '.' not in host:
        raise ValueError('Local/test/placeholder hostname cannot be used for formal release')
    if any(host == d or host.endswith('.' + d) for d in
           ('example.com', 'example.net', 'example.org', 'your-domain.com')):
        raise ValueError('Reserved example hostname cannot be used for formal release')
    return host


def validate_hosts(hosts, *, test_only=False):
    if not isinstance(hosts, (list, tuple)) or not hosts:
        raise ValueError('Explicit asset_hosts are required')
    result = [validate_host(host, test_only=test_only) for host in hosts]
    if len(result) != len(set(result)):
        raise ValueError('Duplicate asset host')
    return result


def public_release_url(url, *, test_only=False):
    if not isinstance(url, str) or not url or url != url.strip() or any(ord(c) < 33 for c in url):
        raise ValueError('A nonempty explicit HTTPS release URL is required')
    parsed = urlsplit(url)
    try:
        parsed.port
    except ValueError:
        raise ValueError('Invalid release URL port') from None
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or parsed.query or '\\' in url
            or PLACEHOLDER.search(unquote(url))):
        raise ValueError('Release URL must be HTTPS without credentials, query, fragment or placeholders')
    validate_host(parsed.hostname, test_only=test_only)
    return url


def config_template():
    return {'format': FORMAT, 'channel': 'preview', 'test_only': False, 'manifest_url': '',
            'asset_hosts': [], 'program_asset_url': '', 'installer_asset_url': ''}


def validate_config(config, *, asset=None, test_only=False):
    if not isinstance(config, dict) or config.get('format') != FORMAT:
        raise ValueError('Invalid release configuration format')
    if config.get('channel') not in {'preview', 'stable'}:
        raise ValueError('Explicit preview/stable channel label is required')
    if type(config.get('test_only', False)) is not bool or config.get('test_only', False) != test_only:
        raise ValueError('Test configuration requires explicit --test-only and test_only=true together')
    public_release_url(config.get('manifest_url'), test_only=test_only)
    hosts = validate_hosts(config.get('asset_hosts'), test_only=test_only)
    if not any(config.get(name) for name in ['program_asset_url', 'installer_asset_url']):
        raise ValueError('At least one explicit release asset URL is required')
    for name in ['program_asset_url', 'installer_asset_url']:
        value = config.get(name, '')
        if value:
            public_release_url(value, test_only=test_only)
            if urlsplit(value).hostname not in hosts:
                raise ValueError(name + ' host must be explicitly pinned')
    if asset and not config.get(asset + '_asset_url'):
        raise ValueError('Explicit ' + asset + '_asset_url is required')
    return {**config, 'asset_hosts': hosts}


def _safe_relative(name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        return False
    path = PurePosixPath(name)
    return (not path.is_absolute() and str(path) == name and '..' not in path.parts
            and all(p and p.rstrip(' .') == p and not any(ord(c) < 32 for c in p)
                    for p in path.parts))


def public_source_members(source):
    source = Path(source).resolve()
    names = [n.strip() for n in (source / 'public-source-files.txt').read_text('utf-8-sig').splitlines()
             if n.strip() and not n.lstrip().startswith('#')]
    if not names or len(names) != len({n.casefold() for n in names}):
        raise ValueError('Public source allowlist is empty or has duplicate/aliased entries')
    for name in names:
        path = source / name
        if (not _safe_relative(name) or not path.is_file() or path.is_symlink()
                or not path.resolve().is_relative_to(source)
                or any(p.is_symlink() for p in path.parents if p.is_relative_to(source))):
            raise ValueError('Public allowlist contains missing, unsafe or linked member: ' + name)
    return sorted(names)


def program_version(program):
    if __package__:
        from .release_version import check
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from release_version import check
    # Product-only releases retain the frozen shell's mutually consistent build
    # version. The shared checker still validates VERSION/pyproject/__init__.
    return check(program, scope='program')['version']


def trust_key(program):
    try:
        value = base64.b64decode((Path(program) / 'knowledge_bridge/update-public-key.txt')
                                .read_text('ascii').strip(), validate=True)
    except Exception:
        raise ValueError('Invalid installed Ed25519 trust key') from None
    if len(value) != 32:
        raise ValueError('Invalid installed Ed25519 trust key length')
    return value


def native_inventory(program):
    program = Path(program).resolve()
    allowed = set(public_source_members(program))
    names = set(NATIVE_FILES)
    for name in NATIVE_DIRS:
        directory = program / name
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError('Native build inputs incomplete; use a full installer')
        names.update(p.relative_to(program).as_posix() for p in directory.rglob('*') if p.is_file())
    rows = {}
    for name in sorted(names):
        path = program / name
        if (name not in allowed or not path.is_file() or path.is_symlink()
                or not path.resolve().is_relative_to(program)
                or any(p.is_symlink() for p in path.parents if p.is_relative_to(program))):
            raise ValueError('Native build inputs incomplete, unreviewed or linked; use a full installer')
        rows[name] = {'sha256': file_digest(path), 'bytes': path.stat().st_size}
    return rows


def runtime_inventory(path):
    path = Path(path)
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8-sig') as stream:
        value = json.load(stream)
    if isinstance(value, dict) and isinstance(value.get('files'), (list, dict)):
        value = value['files']
    if isinstance(value, dict):
        rows = [dict(row, path=name) for name, row in value.items()]
    elif isinstance(value, list):
        rows = value
    else:
        raise ValueError('Runtime manifest must contain regular file path/sha256/bytes rows')
    result, seen = {}, set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid runtime inventory row')
        name, digest, size = row.get('path'), row.get('sha256'), row.get('bytes')
        if (not _safe_relative(name) or not isinstance(digest, str)
                or not re.fullmatch('[0-9a-f]{64}', digest)
                or type(size) is not int or size < 0 or name.casefold() in seen):
            raise ValueError('Invalid or aliased runtime inventory row')
        seen.add(name.casefold())
        result[name] = {'sha256': digest, 'bytes': size}
    if not result:
        raise ValueError('Runtime inventory is empty')
    return result


def package_validators():
    root = str(TOOL_SOURCE)
    if root not in sys.path:
        sys.path.insert(0, root)
    from knowledge_bridge import desktop_update
    if not Path(desktop_update.__file__).resolve().is_relative_to(TOOL_SOURCE):
        raise ValueError('Release validator resolved outside the reviewed tool checkout')
    return desktop_update.inspect_package, desktop_update.safe_member, desktop_update.MAX_BYTES


def verify_program(config, previous, package, envelope, *, test_only=False):
    config = validate_config(config, asset='program', test_only=test_only)
    inspect_package, _, max_bytes = package_validators()
    previous, package, envelope = map(Path, (previous, package, envelope))
    if test_only and trust_key(previous) == trust_key(TOOL_SOURCE):
        raise ValueError('Test-only release must use a synthetic key, not the shipping trust key')
    if envelope.stat().st_size > 128 * 1024:
        raise ValueError('Channel envelope exceeds 128 KiB')
    from knowledge_bridge.online_update import OnlineUpdate
    class OfflineVerifier(OnlineUpdate):
        def channel(self):
            return config
    record = OfflineVerifier(previous, previous).verify(envelope.read_bytes())
    if record.get('test_only', False) != test_only:
        raise ValueError('Signed channel test marker differs from selected release mode')
    if record['url'] != config['program_asset_url']:
        raise ValueError('Signed asset URL differs from release configuration')
    if package.stat().st_size > max_bytes or record['size'] != package.stat().st_size:
        raise ValueError('Package size differs from signed channel')
    if record['sha256'] != file_digest(package):
        raise ValueError('Package SHA256 differs from signed channel')
    try:
        manifest = inspect_package(package, previous)
    except ValueError:
        raise
    except Exception:
        raise ValueError('Program package signature/member verification failed') from None
    if manifest.get('test_only', False) != test_only:
        raise ValueError('Signed package test marker differs from selected release mode')
    from packaging.version import Version
    if Version(manifest['version']) != Version(record['version']):
        raise ValueError('Package version differs from signed channel')
    if Version(record['version']) <= Version(program_version(previous)):
        raise ValueError('Release version must be greater than installed program version')
    if any(name.startswith('desktop/') for name in manifest['files']):
        raise ValueError('Program release contains native build files; use a full installer')
    return {'offline_checks_passed': True, 'formal_preflight_passed': not test_only,
            'test_only': test_only, 'published': False,
            'public_channel_live': False, 'network_checked': False,
            'owner_address_review_required': True, 'kind': 'program-only',
            'version': record['version'], 'package_bytes': package.stat().st_size,
            'package_sha256': record['sha256'], 'files': len(manifest['files']),
            'expires_at': record['expires_at'], 'manifest_url': config['manifest_url'],
            'asset_url': record['url'],
            'limits': 'Offline verification does not establish domain ownership, availability or deployment. Test-only artifacts must never be published.'}


def pe_product_version(path):
    """Read version resources without executing the installer."""
    if os.name != 'nt':
        raise ValueError('PE version check requires Windows; no execution fallback')
    import ctypes
    from ctypes import wintypes
    library = ctypes.WinDLL('version', use_last_error=True)
    library.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    library.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    library.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    library.GetFileVersionInfoW.restype = wintypes.BOOL
    library.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                      ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    library.VerQueryValueW.restype = wintypes.BOOL
    size = library.GetFileVersionInfoSizeW(str(Path(path).resolve()), None)
    if not size:
        raise ValueError('Installer has no readable PE version information')
    data = ctypes.create_string_buffer(size)
    if not library.GetFileVersionInfoW(str(Path(path).resolve()), 0, size, data):
        raise ValueError('Cannot read installer PE version information')
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if not library.VerQueryValueW(data, '\\VarFileInfo\\Translation', ctypes.byref(pointer), ctypes.byref(length)):
        raise ValueError('Installer version translation is missing')
    pairs = ctypes.cast(pointer, ctypes.POINTER(wintypes.WORD))
    translations = [(pairs[i], pairs[i + 1]) for i in range(0, length.value // 2, 2)]
    for language, page in translations:
        query = f'\\StringFileInfo\\{language:04x}{page:04x}\\ProductVersion'
        if library.VerQueryValueW(data, query, ctypes.byref(pointer), ctypes.byref(length)):
            return ctypes.wstring_at(pointer, max(0, length.value - 1)).strip()
    raise ValueError('Installer ProductVersion is missing')


def authenticode(path):
    """Ask Windows for trusted Authenticode status; never install certificates."""
    if os.name != 'nt':
        raise ValueError('Trusted Authenticode verification requires Windows')
    preferred = Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft/WindowsApps/pwsh.exe'
    executable = str(preferred) if preferred.is_file() else shutil.which('pwsh')
    if not executable:
        raise ValueError('PowerShell 7 is required for trusted Authenticode verification')
    script = ("$ErrorActionPreference='Stop';"
              "[Console]::InputEncoding=[Text.UTF8Encoding]::new($false);"
              "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
              "$p=[Console]::In.ReadToEnd();$s=Get-AuthenticodeSignature -LiteralPath $p;"
              "[pscustomobject]@{status=[string]$s.Status;"
              "signer_thumbprint=if($s.SignerCertificate){$s.SignerCertificate.Thumbprint}else{$null};"
              "timestamp_present=($null -ne $s.TimeStamperCertificate)}|ConvertTo-Json -Compress")
    process = subprocess.run([executable, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script],
                             input=str(Path(path).resolve()).encode('utf-8'),
                             capture_output=True, timeout=60, creationflags=0x08000000)
    if process.returncode:
        raise ValueError('Windows Authenticode verification did not complete')
    try:
        result = json.loads(process.stdout.decode('utf-8-sig'))
        if not isinstance(result, dict) or not result.get('status'):
            raise ValueError('Empty Windows signature status')
        return result
    except ValueError:
        raise ValueError('Windows Authenticode result was not readable') from None


def installer_checklist(installer, expected_version, expected_sha256, *, config=None, test_only=False):
    from packaging.version import Version
    path = Path(installer)
    if not re.fullmatch('[0-9a-f]{64}', expected_sha256):
        raise ValueError('Explicit expected installer SHA256 is required')
    if not path.is_file() or file_digest(path) != expected_sha256:
        raise ValueError('Installer SHA256 differs from reviewed manifest')
    with path.open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise ValueError('Installer is not a PE executable')
    name = re.fullmatch(r'Jiedu-(.+)-windows-x64-setup\.exe', path.name)
    if not name or Version(name.group(1)) != Version(expected_version):
        raise ValueError('Installer filename version differs from release version')
    product_version = pe_product_version(path)
    if Version(product_version) != Version(expected_version):
        raise ValueError('Installer PE ProductVersion differs from release version')
    try:
        signature = authenticode(path)
    except (ValueError, subprocess.SubprocessError) as error:
        signature = {'status': 'NotChecked', 'reason': str(error)}
    configured = validate_config(config, asset='installer', test_only=test_only) if config is not None else None
    blockers = []
    if not configured:
        blockers.append('Formal public release configuration is absent')
    warnings = []
    if signature.get('status') != 'Valid' or not signature.get('signer_thumbprint'):
        warnings.append('Installer has no confirmed Windows-trusted Authenticode signature; Windows may warn about an unknown publisher. Review source and SHA256 before manual download/run.')
    return {'candidate_integrity_checks_passed': True, 'offline_checks_passed': not blockers,
            'formal_preflight_passed': not blockers and not test_only,
            'test_only': test_only, 'blocking_items': blockers,
            'published': False, 'public_channel_live': False, 'network_checked': False,
            'owner_address_review_required': True, 'kind': 'complete-installer',
            'version': expected_version, 'pe_product_version': product_version,
            'installer_name': path.name, 'installer_bytes': path.stat().st_size,
            'installer_sha256': expected_sha256, 'authenticode': signature,
            'authenticode_required': False, 'warnings': warnings,
            'installer_execution': 'none by this checklist; automatic update additionally requires a signed channel and cooperative handoff',
            'asset_url': configured['installer_asset_url'] if configured else '',
            'limits': 'This checklist is not a .jiedu update or permission to publish/execute. Authenticode is optional and does not replace SHA256/version checks. Windows trust services may check certificate chains.'}


def verify_installer(config, previous, installer, envelope, *, test_only=False):
    config = validate_config(config, asset='installer', test_only=test_only)
    package_validators()
    from knowledge_bridge.online_update import OnlineUpdate
    from knowledge_bridge.full_installer_update import inspect_installer
    class OfflineVerifier(OnlineUpdate):
        def channel(self):
            return config
    previous, installer, envelope = map(Path, (previous, installer, envelope))
    if test_only and trust_key(previous) == trust_key(TOOL_SOURCE):
        raise ValueError('Test-only release requires a synthetic key')
    record = OfflineVerifier(previous, previous).verify(envelope.read_bytes())
    if record['kind'] != 'complete-installer' or record['url'] != config['installer_asset_url']:
        raise ValueError('Signed full-installer channel differs from release configuration')
    from packaging.version import Version
    if Version(record['version']) <= Version(program_version(previous)):
        raise ValueError('Release version must be greater than installed product version')
    inspect_installer(installer, record)
    return {'offline_checks_passed': True, 'formal_preflight_passed': not test_only,
            'kind': 'complete-installer', 'test_only': test_only, 'version': record['version'],
            'sha256': record['sha256'], 'bytes': record['size'], 'published': False,
            'public_channel_live': False, 'network_checked': False,
            'authenticode_required': False, 'installer_execution': 'none',
            'limits': 'Checks signed origin, SHA256 and version, not domain ownership or installation lifecycle.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    template = commands.add_parser('template')
    template.add_argument('--output', required=True)
    config = commands.add_parser('config-check')
    config.add_argument('--config', required=True)
    program = commands.add_parser('verify-program')
    for name in ['config', 'previous', 'package', 'envelope']:
        program.add_argument('--' + name, required=True)
    installer = commands.add_parser('installer-checklist')
    for name in ['installer', 'version', 'sha256']:
        installer.add_argument('--' + name, required=True)
    installer.add_argument('--config')
    full = commands.add_parser('verify-installer')
    for name in ['config', 'previous', 'installer', 'envelope']:
        full.add_argument('--' + name, required=True)
    for command in [config, program, installer, full]:
        command.add_argument('--report')
        command.add_argument('--test-only', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.action == 'template':
            with Path(args.output).open('x', encoding='utf-8') as stream:
                json.dump(config_template(), stream, ensure_ascii=False, indent=2)
            return 0
        configuration = (json.loads(Path(args.config).read_text('utf-8-sig'))
                         if getattr(args, 'config', None) else None)
        if args.action == 'config-check':
            validate_config(configuration, test_only=args.test_only)
            result = {'offline_checks_passed': True, 'formal_preflight_passed': not args.test_only,
                      'test_only': args.test_only, 'network_checked': False,
                      'public_channel_live': False, 'published': False,
                      'owner_address_review_required': True}
        elif args.action == 'verify-program':
            result = verify_program(configuration, args.previous, args.package, args.envelope,
                                    test_only=args.test_only)
        elif args.action == 'verify-installer':
            result = verify_installer(configuration, args.previous, args.installer, args.envelope,
                                      test_only=args.test_only)
        else:
            result = installer_checklist(args.installer, args.version, args.sha256,
                                         config=configuration, test_only=args.test_only)
        if getattr(args, 'report', None):
            with Path(args.report).open('x', encoding='utf-8') as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        passed = result.get('offline_checks_passed') if args.test_only else result.get('formal_preflight_passed')
        return 0 if passed else 2
    except (ValueError, OSError) as error:
        parser.exit(2, str(error) + '\n')


if __name__ == '__main__':
    sys.exit(main())
