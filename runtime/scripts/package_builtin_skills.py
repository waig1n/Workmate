#!/usr/bin/env python3
"""Package top-level Skill sources for Workmate's ZIP upload workflow."""
import argparse
from pathlib import Path
import re
import zipfile

import yaml

from workmate_product.skills.catalog import builtin_root

ROOT = builtin_root()
EXCLUDED = {'evals', '__pycache__', '.git', 'node_modules'}


def package(source: Path, destination: Path) -> Path:
    if source.is_symlink():
        raise ValueError("Skill source must not be a symlink")
    lines = (source / 'SKILL.md').read_text(encoding='utf-8').splitlines()
    if not lines or lines[0] != '---' or '---' not in lines[1:]:
        raise ValueError(f'{source.name}: missing frontmatter')
    metadata = yaml.safe_load('\n'.join(lines[1:lines.index('---', 1)]))
    if not isinstance(metadata, dict) or metadata.get('name') != source.name:
        raise ValueError(f'{source.name}: name must match folder')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', source.name):
        raise ValueError('Invalid skill name')
    if not isinstance(metadata.get('description'), str) or not metadata['description'].strip():
        raise ValueError('Nonempty description required')
    members = []
    for path in sorted(source.rglob('*')):
        relative = path.relative_to(source)
        if any(part in EXCLUDED for part in relative.parts) or path.suffix == '.pyc':
            continue
        if path.is_symlink():
            raise ValueError(f'Symlink not allowed: {relative}')
        if path.is_file():
            if path.name == 'SKILL.md' and relative != Path('SKILL.md'):
                raise ValueError(f'Nested SKILL.md would confuse the upload parser: {relative}')
            members.append(path)
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / f'{source.name}.zip'
    if output.is_relative_to(source.resolve()):
        raise ValueError('Package output must be outside its source directory')
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in members:
            archive.write(path, path.relative_to(source.parent))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    # No catalog hardcoding: only directories with a root SKILL.md are included.
    for entry in sorted(ROOT.glob('*/SKILL.md')):
        print(package(entry.parent, args.output_dir.resolve()))


if __name__ == '__main__':
    main()
