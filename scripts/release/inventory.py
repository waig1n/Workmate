"""Record installed application sources without rewriting wheel contents."""
import argparse
import hashlib
import json
from pathlib import Path

from .policy import PACKAGES, classify


def inventory(root, report_path):
    root = Path(root)
    files = []
    for package in PACKAGES:
        for path in sorted((root / package).rglob('*')):
            if path.is_symlink():
                raise ValueError(f'Symlink in installed application: {path}')
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            mode = classify(relative)
            if mode == 'omit':
                continue
            files.append({'path': relative, 'mode': mode,
                          'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    if not any(entry['mode'] == 'source' for entry in files):
        raise ValueError('No installed application sources found')
    Path(report_path).write_text(json.dumps({'files': files}, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    inventory(args.root, args.report)
