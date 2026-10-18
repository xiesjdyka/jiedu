"""Build a reviewed allowlisted program update; never upload or publish.

Runtime inventories and native inputs must match the previous installation.
Keep signing keys and output outside the source tree. Component changes need a
complete installer, even when requirements.lock.txt has not changed.
"""
import argparse
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import zipfile

if __package__:
    from . import update_release as release
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import update_release as release


def build(source, previous, key_path, output, url, hosts, notes='', *,
          runtime_manifest=None, previous_runtime_manifest=None, expires_days=30,
          test_only=False, native_exe=None, previous_native_exe=None):
    source, previous, key_path, output = [Path(p).resolve() for p in
                                         (source, previous, key_path, output)]
    if key_path.is_relative_to(source) or key_path.is_relative_to(output):
        raise ValueError('Signing key must be outside source and release output')
    if output.is_relative_to(source):
        raise ValueError('Release output must be outside source')
    release.public_release_url(url, test_only=test_only)
    hosts = release.validate_hosts(hosts, test_only=test_only)
    from urllib.parse import urlsplit
    if urlsplit(url).hostname not in hosts:
        raise ValueError('Asset hostname must be explicitly pinned')
    if not 1 <= expires_days <= 365:
        raise ValueError('Channel expiry must be between 1 and 365 days')
    target = release.program_version(source)
    from packaging.version import Version
    if Version(target) <= Version(release.program_version(previous)):
        raise ValueError('Version must increase')
    if (source / 'requirements.lock.txt').read_bytes() != (previous / 'requirements.lock.txt').read_bytes():
        raise ValueError('Dependencies changed: build a full installer')
    native = release.native_inventory(source)
    if native != release.native_inventory(previous):
        raise ValueError('Native build inputs changed: build a full installer')
    if not runtime_manifest or not previous_runtime_manifest:
        raise ValueError('Both packaged-runtime inventories are required; otherwise use a full installer')
    runtime = release.runtime_inventory(runtime_manifest)
    if runtime != release.runtime_inventory(previous_runtime_manifest):
        raise ValueError('Packaged runtime changed: build a full installer')
    if not native_exe or not previous_native_exe:
        raise ValueError('Both native EXE files are required; otherwise use a full installer')
    native_exe, previous_native_exe = Path(native_exe), Path(previous_native_exe)
    if not native_exe.is_file() or not previous_native_exe.is_file():
        raise ValueError('Native EXE file is missing; use a full installer')
    shell = {'sha256': release.file_digest(native_exe), 'bytes': native_exe.stat().st_size}
    previous_shell = {'sha256': release.file_digest(previous_native_exe),
                      'bytes': previous_native_exe.stat().st_size}
    if shell != previous_shell:
        raise ValueError('Native EXE bytes changed: build a full installer')
    public_key = release.trust_key(previous)
    if test_only and public_key == release.trust_key(release.TOOL_SOURCE):
        raise ValueError('Test-only release must use a synthetic key, not the shipping trust key')
    if release.trust_key(source) != public_key:
        raise ValueError('Trust key changed: use a full installer and explicit key migration')
    inspect_package, safe_member, max_bytes = release.package_validators()
    files = {}
    for name in release.public_source_members(source):
        if safe_member(name) and not name.startswith('desktop/'):
            files[name] = (source / name).read_bytes()
    if not {'VERSION', 'pyproject.toml', 'requirements.lock.txt'}.issubset(files):
        raise ValueError('Public allowlist must include VERSION, pyproject.toml and requirements.lock.txt')
    if sum(map(len, files.values())) > max_bytes - 256 * 1024:
        raise ValueError('Program update exceeds the size limit including metadata')
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError('Expected Ed25519 signing key')
    if key.public_key().public_bytes_raw() != public_key:
        raise ValueError('Signing key does not match installed trust key')
    manifest = json.dumps({
        'format': 'jiedu-program-update-v1', 'version': target, 'test_only': test_only,
        'files': {n: hashlib.sha256(v).hexdigest() for n, v in files.items()},
        'compatibility': {
            'native_inputs_sha256': release.inventory_digest(native),
            'packaged_runtime_sha256': release.inventory_digest(runtime),
            'native_exe_sha256': shell['sha256'], 'native_exe_bytes': shell['bytes'],
            'scope': 'program-only; native shell and packaged runtime unchanged',
        },
    }, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    output.mkdir(parents=True, exist_ok=True)
    package = output / ('jiedu-' + target + '.jiedu')
    channel = output / 'channel.json'
    if package.exists() or channel.exists():
        raise ValueError('Release artifacts already exist; use a new empty output directory')
    try:
        with zipfile.ZipFile(package, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json', manifest)
            archive.writestr('signature.txt', base64.b64encode(key.sign(manifest)))
            for name, raw in files.items():
                archive.writestr('program/' + name, raw)
        inspect_package(package, previous)
        record = {
            'format': 'jiedu-update-channel-v1', 'version': target, 'url': url,
            'test_only': test_only,
            'sha256': release.file_digest(package), 'size': package.stat().st_size,
            'expires_at': (datetime.now(timezone.utc) + timedelta(days=expires_days)).isoformat(),
            'notes': notes,
        }
        payload = json.dumps(record, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        with channel.open('x', encoding='utf-8') as stream:
            json.dump({'payload': base64.b64encode(payload).decode('ascii'),
                       'signature': base64.b64encode(key.sign(payload)).decode('ascii')},
                      stream, indent=2)
    except BaseException:
        package.unlink(missing_ok=True)
        channel.unlink(missing_ok=True)
        raise
    return package


def build_installer(source, installer, key_path, output, config, notes='', *,
                    expires_days=30, test_only=False):
    """Sign the full installer channel after version/hash/host preflight.

    The installer is not executed, copied or represented as Authenticode-signed.
    Installed clients still require their existing trust key to match this key.
    """
    source, installer, key_path, output = map(lambda p: Path(p).resolve(),
                                             (source, installer, key_path, output))
    if (key_path.is_relative_to(source) or key_path.is_relative_to(output)
            or output.is_relative_to(source)):
        raise ValueError('Signing key and output must be separate from source and each other')
    config = release.validate_config(config, asset='installer', test_only=test_only)
    if not 1 <= expires_days <= 365:
        raise ValueError('Channel expiry must be between 1 and 365 days')
    target = release.program_version(source)
    from packaging.version import Version
    if config.get('channel') == 'stable' and Version(target).is_prerelease:
        raise ValueError('Stable channel cannot publish a prerelease version')
    report = release.installer_checklist(installer, target, release.file_digest(installer),
                                         config=config, test_only=test_only)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey) or key.public_key().public_bytes_raw() != release.trust_key(source):
        raise ValueError('Signing key does not match the reviewed source trust key')
    if test_only and release.trust_key(source) == release.trust_key(release.TOOL_SOURCE):
        raise ValueError('Test-only release requires a synthetic trust key')
    from knowledge_bridge.full_installer_update import MAX_INSTALLER_BYTES
    if not 0 < installer.stat().st_size <= MAX_INSTALLER_BYTES:
        raise ValueError('Full installer exceeds the client size limit')
    record = {'format': 'jiedu-update-channel-v1', 'kind': 'complete-installer',
              'version': target, 'url': config['installer_asset_url'],
              'installer_name': installer.name, 'size': installer.stat().st_size,
              'sha256': report['installer_sha256'], 'test_only': test_only,
              'expires_at': (datetime.now(timezone.utc) + timedelta(days=expires_days)).isoformat(),
              'notes': notes}
    payload = json.dumps(record, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    output.mkdir(parents=True, exist_ok=True)
    path = output / 'update.json'
    with path.open('x', encoding='utf-8') as stream:
        json.dump({'payload': base64.b64encode(payload).decode('ascii'),
                   'signature': base64.b64encode(key.sign(payload)).decode('ascii')}, stream, indent=2)
    return path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=['program-only','complete-installer'], default='program-only')
    for name in ['source', 'key', 'output']:
        parser.add_argument('--' + name, required=True)
    for name in ['previous', 'url',
                 'runtime-manifest', 'previous-runtime-manifest',
                 'native-exe', 'previous-native-exe']:
        parser.add_argument('--' + name)
    parser.add_argument('--host', action='append')
    parser.add_argument('--installer')
    parser.add_argument('--config')
    parser.add_argument('--notes', default='')
    parser.add_argument('--expires-days', type=int, default=30)
    parser.add_argument('--test-only', action='store_true')
    args = parser.parse_args()
    try:
        if args.kind == 'complete-installer':
            if not args.installer or not args.config:
                raise ValueError('Full installer signing requires --installer and --config')
            print(build_installer(args.source, args.installer, args.key, args.output,
                                  json.loads(Path(args.config).read_text('utf-8-sig')),
                                  args.notes, expires_days=args.expires_days, test_only=args.test_only))
            sys.exit(0)
        if not args.previous or not args.url or not args.host:
            raise ValueError('Program update requires --previous, --url and --host')
        print(build(args.source, args.previous, args.key, args.output,
                    args.url, args.host, args.notes,
                    runtime_manifest=args.runtime_manifest,
                    previous_runtime_manifest=args.previous_runtime_manifest,
                    expires_days=args.expires_days, test_only=args.test_only,
                    native_exe=args.native_exe, previous_native_exe=args.previous_native_exe))
    except (ValueError, OSError) as error:
        parser.exit(2, str(error) + '\n')
