"""Prepare this distribution's OCR runtime from verified local wheels, offline.

The release preparation step downloads wheels separately from official PyPI.
This script never downloads and is not called during document recognition.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile


def prepare(root):
    repository=Path(__file__).resolve().parents[1]
    manifest=json.loads((repository/'ocr-resources.lock.json').read_text('utf-8'))
    wheels=root/'wheels'
    for row in manifest.get('source_archives',[]):
        path=root/row['file']
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:
            raise RuntimeError('Redistribution source missing or checksum mismatch: '+row['file'])
    for row in manifest['wheels']:
        path=wheels/row['file']
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:
            raise RuntimeError('Offline wheel missing or checksum mismatch: '+row['file'])
    python=root/'venv'/'Scripts'/'python.exe'
    if not python.exists():subprocess.run([sys.executable,'-m','venv',str(root/'venv')],check=True)
    subprocess.run([str(python),'-m','pip','install','--no-index','--no-deps',
                    *[str(wheels/r['file']) for r in manifest['wheels']]],check=True)
    (root/'models').mkdir(exist_ok=True)
    for row in manifest['models']:
        with zipfile.ZipFile(wheels/row['wheel']) as z:data=z.read(row['member'])
        if hashlib.sha256(data).hexdigest()!=row['sha256']:raise RuntimeError('Model checksum mismatch')
        (root/'models'/row['file']).write_bytes(data)
    for row in manifest['wheels']:
        with zipfile.ZipFile(wheels/row['file']) as z:
            for member in z.namelist():
                if any(part in member.lower() for part in ('license','notice','copying','copyright')) and not member.endswith('/'):
                    target=root/'licenses'/row['package']/member.replace('/','__')
                    target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(z.read(member))
    subprocess.run([str(python),'-m','pip','check'],check=True)
    print(json.dumps({'status':'offline_ready','root':str(root),'models':len(manifest['models'])}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]/'runtime'/'ocr')
    prepare(parser.parse_args().root.resolve())
