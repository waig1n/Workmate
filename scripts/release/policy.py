"""Ownership and source inventory policy for installed application packages."""
from pathlib import PurePosixPath

PACKAGES = ('app', 'scripts', 'workmate_runtime', 'workmate_product', 'workmate_contracts')
RESOURCE = 'workmate_product/skills/builtin_skills/'


def normalize_platform(value):
    aliases = {'linux/x86_64': 'linux/amd64', 'linux/aarch64': 'linux/arm64',
               'linux/arm64/v8': 'linux/arm64'}
    value = aliases.get(value.strip(), value.strip())
    if value not in {'linux/amd64', 'linux/arm64'}:
        raise ValueError(f'Unsupported platform: {value}')
    return value


def classify(name):
    path = PurePosixPath(name)
    if '__pycache__' in path.parts:
        return 'omit'
    if name.startswith(RESOURCE):
        return 'resource'
    if path.suffix in {'.c', '.o'}:
        return 'omit'
    return 'source' if path.suffix == '.py' else 'resource'
