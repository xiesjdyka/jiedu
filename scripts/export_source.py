"""Export only reviewed, explicitly listed source files; never copy a workspace."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile

FORBIDDEN_PARTS = {'.git', '.jiedu', '.venv312', '__pycache__', 'data', 'runtime',
                   'node_modules', 'target', 'artifacts', '90_原始档案'}
FORBIDDEN_NAMES = {'config.json', 'records_v1.json', '.env'}
FORBIDDEN_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.gguf', '.key', '.pem', '.log', '.pyc'}


def approved_path(root, name):
    parts = PurePosixPath(name)
    if (not name or '\\' in name or ':' in name or parts.is_absolute()
            or any(p in {'.', '..'} for p in name.split('/'))
            or any(p.lower() in FORBIDDEN_PARTS for p in parts.parts)
            or parts.name.lower() in FORBIDDEN_NAMES
            or parts.suffix.lower() in FORBIDDEN_SUFFIXES):
        raise ValueError('Disallowed source member: ' + name)
    if parts.parts[0] == 'memory_compiler' and (len(parts.parts) != 2 or parts.suffix != '.py'):
        raise ValueError('Private compiler data is never a source member')
    path = root.joinpath(*parts.parts)
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Source member escapes root: ' + name)
    if not path.is_file():
        raise ValueError('Missing approved source member: ' + name)
    return path


def export(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    names = [line.strip() for line in (root/'public-source-files.txt').read_text('utf-8').splitlines()
             if line.strip() and not line.startswith('#')]
    if len(names) != len(set(names)):
        raise ValueError('Duplicate approved source member')
    files = [(n, approved_path(root, n)) for n in names]
    if output in [p.resolve() for _, p in files] or output.exists():
        raise ValueError('Choose a new output filename; existing files are never overwritten')
    manifest = [{'path':n, 'bytes':p.stat().st_size, 'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
                for n,p in files]
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED) as archive:
        for row, (_, path) in zip(manifest, files):
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != row['sha256']:
                raise RuntimeError('Source changed during export')
            archive.writestr('jiedu/'+row['path'], data)
        archive.writestr('jiedu/release-manifest.json', json.dumps(
            {'format':'jiedu-source-manifest-v1','files':manifest}, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('Archive integrity failure')
        for row in manifest:
            if hashlib.sha256(archive.read('jiedu/'+row['path'])).hexdigest() != row['sha256']:
                raise RuntimeError('Archive hash mismatch')
    return {'files':len(files),'bytes':output.stat().st_size,
            'sha256':hashlib.sha256(output.read_bytes()).hexdigest()}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args=parser.parse_args()
    print(json.dumps(export(Path(__file__).resolve().parents[1], args.output), indent=2))
