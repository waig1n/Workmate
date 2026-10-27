"""Run with python -m scripts.release; no implicit push or service restart."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import zipapp
import zipfile
import tarfile

from .audit import audit_archive, audit_name
from .core import (CHECKS, ROLES, application_sbom, release_compose, daemon_platform, digest, inspect_image,
                   load_manifest, require_acceptance, require_delivery, run, save_json)
from .policy import normalize_platform

ROOT = Path(__file__).resolve().parents[2]
SKIP = {'.git', '.venv', 'node_modules', '__pycache__', '.next', '.pytest_cache',
        '.mypy_cache', '.ruff_cache', '.coverage', 'htmlcov', 'tests', 'logs', 'data',
        'output', 'outputs', 'out', 'work', 'working', 'workspace-data', 'artifact-storage',
        'build', 'dist', 'coverage', 'test-results', 'playwright-report',
        '.codex', '.agents', '.claude', '.zcode', '.playwright-cli'}
INPUTS = ('backend', 'runtime', 'packages', 'frontend', 'scripts/release')
ASSETS = ('LICENSE', 'NOTICE', 'README.md', 'README.zh-CN.md', 'docker-compose.yml', '.env.example', 'config/workmate.yaml',
          'docs/configuration.md',
    'config/searxng/settings.yml', 'scripts/init-db.sql',
          'scripts/init_knowledge_bucket.py', 'scripts/init_storage.py',
          'scripts/migrate_product_objects.py')
DOCUMENTATION_PAGES = (
    'README.md',
    'administration.md',
    'api.md',
    'architecture.md',
    'configuration-reference.md',
    'configuration.md',
    'deployment.md',
    'development.md',
    'getting-started.md',
    'integrations.md',
    'limitations.md',
    'models.md',
    'runtime.md',
    'third-party.md',
    'tooling.md',
    'user-guide.md',
)
ASSETS += tuple(
    f'docs/{language}/{name}'
    for language in ('en', 'zh-CN')
    for name in DOCUMENTATION_PAGES
)


def files_for_context(root):
    for prefix in INPUTS:
        for path in sorted((root / prefix).rglob('*')):
            rel = path.relative_to(root)
            if any(p in SKIP or p.startswith('.env') for p in rel.parts):
                continue
            if path.is_symlink():
                raise ValueError(f'Symlink not allowed in release context: {rel}')
            if path.is_file() and path.suffix not in {'.pyc', '.so', '.o', '.tsbuildinfo', '.log', '.db', '.sqlite', '.sqlite3'}:
                audit_name(rel.as_posix())
                yield rel
    for name in ASSETS:
        if (root / name).is_symlink():
            raise ValueError(f'Symlink not allowed in delivery assets: {name}')
        yield Path(name)


def check(args):
    platform = daemon_platform()
    if args.platform and normalize_platform(args.platform) != platform:
        raise ValueError('Target differs from Docker daemon; run natively on the target host')
    run(['docker', 'buildx', 'version'])
    files = list(files_for_context(ROOT))
    missing = [str(p) for p in files if not (ROOT / p).is_file()]
    if missing:
        raise ValueError('Missing inputs: ' + ', '.join(missing))
    dirty = bool(run(['git', '--no-optional-locks', 'status', '--porcelain', '--untracked-files=normal'], cwd=ROOT))
    result = {'platform': platform, 'source_files': len(files), 'dirty': dirty,
              'commit': run(['git', 'rev-parse', 'HEAD'], cwd=ROOT),
              'dependency_preflight': 'Wheel availability is enforced during build, not downloaded by check.'}
    print(json.dumps(result, indent=2))
    return result


def base_image(ref, platform):
    run(['docker', 'pull', '--platform', platform, ref], capture=False)
    image = inspect_image(ref, platform)
    if not image.get('RepoDigests'):
        raise ValueError('Base image has no immutable registry digest')
    return image['RepoDigests'][0]


def build(args):
    status = check(args)
    if status['dirty'] and not args.development:
        raise ValueError('Official builds require clean git status; use --development for trials')
    out = args.output.resolve()
    if out.exists():
        raise ValueError('Output already exists; use a new directory for each candidate')
    if ROOT == out or ROOT in out.parents:
        raise ValueError('Keep release output outside the source repository')
    out.mkdir(parents=True)
    manifest = {'schema': 1, **status, 'version': args.version,
                'development': args.development, 'images': {}, 'inputs': {}, 'bases': {}}
    save_json(out / 'release.json', manifest)
    with tempfile.TemporaryDirectory(prefix='workmate-context-') as temporary:
        context = Path(temporary)
        for rel in files_for_context(ROOT):
            dest = context / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, dest)
            manifest['inputs'][str(rel)] = digest(dest)
        if not args.development:
            current = run(['git', '--no-optional-locks', 'status', '--porcelain', '--untracked-files=normal'], cwd=ROOT)
            head = run(['git', 'rev-parse', 'HEAD'], cwd=ROOT)
            if current or head != status['commit']:
                raise ValueError('Source changed while creating the release snapshot')
        # Preserve the precise delivery inputs alongside this candidate, never re-read live config at bundle.
        for name in ASSETS:
            dest = out / 'assets' / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(context / name, dest)
        arch = status['platform'].split('/')[1]
        snapshot_hash = hashlib.sha256(json.dumps(manifest['inputs'], sort_keys=True).encode()).hexdigest()
        manifest['snapshot_sha256'] = snapshot_hash
        for role in args.role or ROLES:
            kind = 'node' if role == 'frontend' else 'python'
            if kind not in manifest['bases']:
                manifest['bases'][kind] = base_image(
                    args.node_base if kind == 'node' else args.python_base, status['platform'])
            tag = f'workmate-{role}:{args.version}-{status["commit"][:12]}-{arch}'
            if args.development:
                tag += '-dev-' + snapshot_hash[:12]
            command = ['docker', 'buildx', 'build', '--builder', 'default', '--load',
                       '--platform', status['platform'], '--target', role, '-t', tag,
                       '--build-arg', f'{kind.upper()}_BASE={manifest["bases"][kind]}',
                       '-f', str(context / f'scripts/release/Dockerfile.{"frontend" if kind == "node" else "python"}')]
            if kind == 'python':
                command += ['--build-arg', 'PROJECT=' + ROLES[role]]
            save_json(out / 'release.json', manifest)
            print(f'Building {role}; log: {out / (role + ".build.log")}', flush=True)
            run(command + [str(context)], capture=False, log=out / f'{role}.build.log')
            image = inspect_image(tag, status['platform'])
            manifest['images'][role] = {'ref': tag, 'id': image['Id']}
            save_json(out / 'release.json', manifest)
    print(f'Built candidate: {out / "release.json"}. Run verify next.')


def verify(args):
    manifest = load_manifest(args.manifest)
    results = {}
    for role, entry in manifest['images'].items():
        with tempfile.TemporaryDirectory(prefix='workmate-audit-') as temporary:
            archive = Path(temporary) / 'image.tar'
            run(['docker', 'save', '-o', str(archive), entry['id']], capture=False)
            results[role] = {'audit': audit_archive(archive)}
        if role == 'frontend':
            script = Path(__file__).with_name('frontend-smoke.cjs')
            results[role]['smoke'] = json.loads(run(['docker', 'run', '--rm', '--network', 'none',
                '--read-only', '--tmpfs', '/tmp', '-v', f'{script}:/app/verify.cjs:ro',
                '--entrypoint', 'node', entry['id'], '/app/verify.cjs']))
        else:
            script = Path(__file__).with_name('smoke.py')
            results[role]['smoke'] = json.loads(run(['docker', 'run', '--rm', '--network', 'none',
                '--read-only', '--tmpfs', '/tmp', '-e', 'HOME=/tmp',
                '-e', 'WORKMATE_CONFIG_FILE=', '-v', f'{script}:/verify.py:ro',
                '--entrypoint', 'python', entry['id'], '/verify.py', role]))
            if role == 'runtime':
                worker_smoke = ROOT / 'runtime/tests/smoke_worker_image.py'
                results[role]['worker_smoke'] = run([
                    'docker', 'run', '--rm', '--network', 'none', '--read-only',
                    '--tmpfs', '/tmp:exec', '--tmpfs', '/data', '--cap-add', 'SYS_ADMIN',
                    '--security-opt', 'seccomp=unconfined', '--security-opt', 'apparmor=unconfined',
                    '-e', 'HOME=/tmp', '-v', f'{worker_smoke}:/worker-smoke.py:ro',
                    '--entrypoint', 'python', entry['id'], '/worker-smoke.py'])
    verification = {'images': {k: v['id'] for k, v in manifest['images'].items()},
                    'packaging': results, 'scope': 'packaging-only'}
    if args.acceptance_report:
        report = json.loads(args.acceptance_report.read_text())
        require_acceptance(manifest, report)
        verification.update(acceptance=report, scope='packaging-and-acceptance')
    save_json(args.manifest, {**manifest, 'verification': verification})
    if not args.acceptance_report:
        save_json(args.manifest.parent / 'acceptance.required.json', {
            'platform': manifest['platform'], 'images': verification['images'],
            'checks': {name: 'pending' for name in sorted(CHECKS)}, 'evidence': ''})
    print('Packaging verification passed. Delivery also requires an image-bound acceptance report.')


def checked_assets(manifest, manifest_path):
    assets = manifest_path.parent / 'assets'
    for name in ASSETS:
        if digest(assets / name) != manifest['inputs'][name]:
            raise ValueError(f'Delivery input changed since build: {name}')
    return assets


def bundle(args):
    import yaml
    manifest = load_manifest(args.manifest)
    require_delivery(manifest)
    out = args.output.resolve()
    if out.exists():
        raise ValueError('Bundle output must be a new directory')
    assets = checked_assets(manifest, args.manifest)
    refs = {role: f'workmate-{role}:{manifest["version"]}' for role in ROLES}
    compose = release_compose(yaml.safe_load((assets / 'docker-compose.yml').read_text()), refs)
    # All infrastructure images stay explicit prerequisites; no misleading offline-complete claim.
    external = sorted({s['image'] for s in compose['services'].values() if s['image'] not in refs.values()})
    out.mkdir(parents=True)
    for name in ASSETS:
        dest = out / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(assets / name, dest)
    (out / 'docker-compose.yml').write_text(yaml.safe_dump(compose, sort_keys=False))
    for role, ref in refs.items():
        run(['docker', 'tag', manifest['images'][role]['id'], ref])
    arch = manifest['platform'].split('/')[1]
    archive = out / f'workmate-{manifest["version"]}-linux-{arch}.tar'
    run(['docker', 'save', '-o', str(archive), *refs.values()], capture=False)
    bundled = {**manifest, 'archive': {'file': archive.name, 'sha256': digest(archive)},
               'external_images': external, 'offline_complete': False}
    save_json(out / 'release.json', bundled)
    save_json(out / 'package-inventory.json', manifest['verification']['packaging'])
    save_json(out / 'sbom.cdx.json', application_sbom(manifest))
    (out / 'README.txt').write_text(
        'Workmate application-only bundle. Infrastructure images are not included.\n'
        'Run python3 import-images.pyz to validate checksums, architecture and IDs before loading.\n'
        'Configure .env from .env.example before starting Compose manually.\n'
        'Read docs/configuration.md for the configuration manual matching this release snapshot.\n'
        'Design cross-links in that manual are available in the matching source repository revision.\n'
        'Third-party license files remain in the application images and package metadata.\n')
    with tempfile.TemporaryDirectory(prefix='workmate-importer-') as temporary:
        importer = Path(temporary)
        (importer / 'release').mkdir()
        for name in ('__init__.py', 'core.py', 'policy.py', 'offline.py'):
            shutil.copyfile(Path(__file__).with_name(name), importer / 'release' / name)
        (importer / '__main__.py').write_text(
            'from pathlib import Path\nimport sys\nfrom release.offline import load\n'
            'load(Path(sys.argv[0]).resolve().parent)\n')
        zipapp.create_archive(importer, out / 'import-images.pyz')
    (out / 'SHA256SUMS').write_text(''.join(f'{digest(p)}  {p.relative_to(out)}\n'
        for p in sorted(out.rglob('*')) if p.is_file() and p.name != 'SHA256SUMS'))
    print(f'Application bundle: {out}. Infrastructure images must be provisioned separately.')


def import_bundle(args):
    from .offline import load
    load(args.directory.resolve())


def publish(args):
    import yaml
    manifest = load_manifest(args.manifest)
    require_delivery(manifest)
    assets = checked_assets(manifest, args.manifest)
    if not re.fullmatch(r'[a-z0-9][a-z0-9.:-]*(?:/[a-z0-9._-]+)*', args.registry):
        raise ValueError('Invalid registry/repository prefix; do not include credentials or a URL scheme')
    arch = manifest['platform'].split('/')[1]
    published = {}
    for role, entry in manifest['images'].items():
        ref = f'{args.registry}/workmate-{role}:{manifest["version"]}-{arch}'
        run(['docker', 'tag', entry['id'], ref])
        run(['docker', 'push', ref], capture=False)
        published[role] = {'ref': ref, 'digests': inspect_image(ref, manifest['platform'])['RepoDigests']}
        matching = [d for d in published[role]['digests'] if d.startswith(ref.rsplit(':', 1)[0] + '@')]
        if not matching:
            raise ValueError(f'No pushed repository digest found: {ref}')
        published[role]['pinned_ref'] = matching[0]
    save_json(args.manifest.parent / 'published.json', published)
    source = yaml.safe_load((assets / 'docker-compose.yml').read_text())
    compose = release_compose(source, {k: v['pinned_ref'] for k, v in published.items()},
                               pull_policy='missing')
    (assets / 'docker-compose.registry.yml').write_text(yaml.safe_dump(compose, sort_keys=False))
    print('Published single-platform images; Registry host architecture is irrelevant.')


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest='command', required=True)
    child = commands.add_parser('import')
    child.add_argument('--directory', type=Path, required=True)
    for name in ('check', 'build'):
        child = commands.add_parser(name)
        child.add_argument('--platform', choices=['linux/amd64', 'linux/arm64'])
        if name == 'build':
            child.add_argument('--output', type=Path, required=True)
            child.add_argument('--version', default='2.0')
            child.add_argument('--role', action='append', choices=list(ROLES))
            child.add_argument('--development', action='store_true')
            child.add_argument('--python-base', default='python:3.12-slim-bookworm')
            child.add_argument('--node-base', default='node:22-alpine')
    for name in ('verify', 'bundle', 'publish'):
        child = commands.add_parser(name)
        child.add_argument('--manifest', type=Path, required=True)
        if name == 'verify':
            child.add_argument('--acceptance-report', type=Path)
        if name == 'bundle':
            child.add_argument('--output', type=Path, required=True)
        if name == 'publish':
            child.add_argument('--registry', required=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if hasattr(args, 'version') and not re.fullmatch(r'[0-9][a-zA-Z0-9_.-]{0,39}', args.version):
            raise ValueError('Invalid version tag')
        if args.command == 'import':
            import_bundle(args)
        else:
            globals()[args.command](args)
        return 0
    except (ValueError, OSError, KeyError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f'error: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
