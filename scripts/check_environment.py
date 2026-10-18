"""Verify exact installed versions and Git revisions after installing the complete lock."""
import importlib.metadata
import json
from pathlib import Path
import re
import sys


def check(root):
    errors=[]; checked=0
    for line in (Path(root)/'requirements.lock.txt').read_text('utf-8').splitlines():
        line=line.strip()
        if not line or line.startswith('#'):continue
        pin=re.fullmatch(r'([\w.-]+)==([^ ;]+)',line)
        vcs=re.fullmatch(r'([\w.-]+) @ git\+(https://[^@]+)@([a-f0-9]{40})',line)
        if not pin and not vcs:
            errors.append('Unsupported lock entry');continue
        name=pin[1] if pin else vcs[1]
        try:
            distribution=importlib.metadata.distribution(name)
            if pin and distribution.version != pin[2]:errors.append(name+': version mismatch')
            if vcs:
                direct=json.loads(distribution.read_text('direct_url.json') or '{}')
                if direct.get('url')!=vcs[2] or direct.get('vcs_info',{}).get('commit_id')!=vcs[3]:
                    errors.append(name+': source revision mismatch')
            checked+=1
        except importlib.metadata.PackageNotFoundError:errors.append(name+': missing')
    return {'checked':checked,'errors':errors}


if __name__=='__main__':
    result=check(Path(__file__).resolve().parents[1])
    print(json.dumps(result,indent=2))
    sys.exit(bool(result['errors']))
