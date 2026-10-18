"""Read product identity without starting services or changing application state."""
import argparse
import json
from pathlib import Path
import re
import tomllib

from packaging.version import Version


def version_info(program):
    program=Path(program)
    python_version=tomllib.loads((program/'pyproject.toml').read_text('utf-8'))['project']['version']
    marker=program/'VERSION'
    product_version=marker.read_text('ascii').strip() if marker.is_file() else python_version
    if marker.is_file() and not re.fullmatch(r'\d+\.\d+\.\d+(?:-rc\.\d+)?',product_version):
        raise ValueError('Invalid product VERSION')
    if Version(product_version)!=Version(python_version):
        raise ValueError('VERSION and Python package metadata disagree')
    return {'product_version':product_version,'python_version':python_version}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--program',type=Path,default=Path(__file__).resolve().parents[1])
    args=parser.parse_args()
    print(json.dumps(version_info(args.program),ensure_ascii=False))
