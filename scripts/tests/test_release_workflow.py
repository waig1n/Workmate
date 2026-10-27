"""Exercise orchestration without pushing images or starting product services."""
import argparse
import io
import json
from pathlib import Path
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release import __main__ as cli
from release import core, offline


def candidate():
    images = {role: {'id': 'sha256:' + role, 'ref': role} for role in core.ROLES}
    ids = {role: entry['id'] for role, entry in images.items()}
    return {'schema': 1, 'platform': 'linux/amd64', 'version': '2.0', 'images': images,
            'verification': {'images': ids, 'packaging': {}, 'acceptance': {
                'platform': 'linux/amd64', 'images': ids, 'evidence': 'test-run',
                'checks': {name: 'passed' for name in core.CHECKS}}}}


def test_publish_uses_platform_suffix_not_registry_host_architecture(monkeypatch, tmp_path):
    manifest = candidate()
    monkeypatch.setattr(cli, 'load_manifest', lambda _: manifest)
    calls = []
    monkeypatch.setattr(cli, 'run', lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(cli, 'inspect_image', lambda ref, _: {'RepoDigests': [ref.rsplit(':', 1)[0] + '@sha256:a']})
    (tmp_path / 'assets').mkdir()
    (tmp_path / 'assets/docker-compose.yml').write_text('services: {}')
    monkeypatch.setattr(cli, 'ASSETS', ('docker-compose.yml',))
    manifest['inputs'] = {'docker-compose.yml': core.digest(tmp_path / 'assets/docker-compose.yml')}
    cli.publish(argparse.Namespace(manifest=tmp_path / 'release.json', registry='registry.example:5000/team'))
    pushes = [cmd for cmd in calls if cmd[:2] == ['docker', 'push']]
    assert len(pushes) == 3
    assert all(cmd[-1].endswith(':2.0-amd64') for cmd in pushes)
    assert (tmp_path / 'published.json').exists()


def test_publish_rejects_development_before_any_mutation(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, 'load_manifest', lambda _: {**candidate(), 'development': True})
    monkeypatch.setattr(cli, 'run', lambda *a, **k: pytest.fail('Unexpected mutation'))
    with pytest.raises(ValueError, match='Development'):
        cli.publish(argparse.Namespace(manifest=tmp_path / 'release.json', registry='registry.test'))


def test_build_requires_clean_source_and_new_output(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, 'check', lambda _: {'dirty': True})
    with pytest.raises(ValueError, match='clean git'):
        cli.build(argparse.Namespace(development=False, output=tmp_path))
    monkeypatch.setattr(cli, 'check', lambda _: {'dirty': False})
    with pytest.raises(ValueError, match='already exists'):
        cli.build(argparse.Namespace(development=False, output=tmp_path))


def test_load_manifest_rejects_other_arch_before_image_lookup(monkeypatch, tmp_path):
    path = tmp_path / 'release.json'
    path.write_text(json.dumps(candidate()))
    monkeypatch.setattr(core, 'daemon_platform', lambda: 'linux/arm64')
    monkeypatch.setattr(core, 'inspect_image', lambda *a: pytest.fail('Wrong architecture'))
    with pytest.raises(ValueError, match='platform differs'):
        core.load_manifest(path)


def test_check_does_not_mutate_or_build(monkeypatch):
    commands = []
    monkeypatch.setattr(cli, 'daemon_platform', lambda: 'linux/amd64')
    monkeypatch.setattr(cli, 'run', lambda cmd, **kw: commands.append(cmd) or '')
    result = cli.check(argparse.Namespace(platform=None))
    assert result['platform'] == 'linux/amd64'
    assert all(cmd[0] == 'git' or cmd == ['docker', 'buildx', 'version'] for cmd in commands)
    with pytest.raises(ValueError, match='differs'):
        cli.check(argparse.Namespace(platform='linux/arm64'))


def test_bundle_captures_only_template_environment(monkeypatch, tmp_path):
    import yaml
    manifest = candidate()
    assets = tmp_path / 'assets'
    assets.mkdir()
    (assets / 'docker-compose.yml').write_text(yaml.safe_dump({'services': {
        'gateway': {'build': '.', 'image': 'old', 'environment': {'TOKEN': '${TOKEN}'}},
        'postgres': {'image': 'postgres:18-alpine'}}}))
    monkeypatch.setattr(cli, 'ASSETS', ('docker-compose.yml',))
    manifest['inputs'] = {'docker-compose.yml': core.digest(assets / 'docker-compose.yml')}
    monkeypatch.setattr(cli, 'load_manifest', lambda _: manifest)
    def run(cmd, **kw):
        if cmd[:2] == ['docker', 'save']:
            Path(cmd[3]).write_bytes(b'fake-docker-save')
    monkeypatch.setattr(cli, 'run', run)
    out = tmp_path / 'bundle'
    cli.bundle(argparse.Namespace(manifest=tmp_path / 'release.json', output=out))
    compose = yaml.safe_load((out / 'docker-compose.yml').read_text())
    assert 'build' not in compose['services']['gateway']
    assert compose['services']['gateway']['environment']['TOKEN'] == '${TOKEN}'
    release = json.loads((out / 'release.json').read_text())
    assert release['offline_complete'] is False
    assert release['external_images'] == ['postgres:18-alpine']


def test_offline_rejects_checksum_before_load(monkeypatch, tmp_path):
    manifest = {**candidate(), 'archive': {'file': 'images.tar', 'sha256': 'wrong'}}
    (tmp_path / 'release.json').write_text(json.dumps(manifest))
    (tmp_path / 'images.tar').write_bytes(b'bad')
    monkeypatch.setattr(offline, 'daemon_platform', lambda: 'linux/amd64')
    monkeypatch.setattr(offline, 'run', lambda *a, **k: pytest.fail('Must not load invalid archive'))
    with pytest.raises(ValueError, match='checksum'):
        offline.load(tmp_path)


def test_offline_validates_config_identity_and_loads_without_start(monkeypatch, tmp_path):
    import hashlib
    manifest = candidate()
    archive = tmp_path / 'images.tar'
    entries, images = [], {}
    with tarfile.open(archive, 'w') as saved:
        for role in core.ROLES:
            data = json.dumps({'os': 'linux', 'architecture': 'amd64', 'role': role}).encode()
            name = hashlib.sha256(data).hexdigest() + '.json'
            images[role] = {'id': 'sha256:' + name[:-5], 'ref': role}
            info = tarfile.TarInfo(name)
            info.size = len(data)
            saved.addfile(info, io.BytesIO(data))
            entries.append({'Config': name, 'RepoTags': [f'workmate-{role}:2.0']})
        data = json.dumps(entries).encode()
        info = tarfile.TarInfo('manifest.json')
        info.size = len(data)
        saved.addfile(info, io.BytesIO(data))
    ids = {k: v['id'] for k, v in images.items()}
    manifest['images'] = images
    manifest['verification']['images'] = ids
    manifest['verification']['acceptance']['images'] = ids
    manifest['archive'] = {'file': archive.name, 'sha256': core.digest(archive)}
    (tmp_path / 'release.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(offline, 'daemon_platform', lambda: 'linux/amd64')
    commands = []
    monkeypatch.setattr(offline, 'run', lambda cmd, **kw: commands.append(cmd))
    monkeypatch.setattr(offline, 'inspect_image', lambda ref, _: {
        'Id': images[ref.removeprefix('workmate-').split(':')[0]]['id']})
    offline.load(tmp_path)
    assert commands == [['docker', 'load', '-i', str(archive)]]


def test_build_uses_snapshot_and_exact_base_digest(monkeypatch, tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'docker-compose.yml').write_text('services: {}')
    (root / 'app.py').write_text('pass')
    monkeypatch.setattr(cli, 'ROOT', root)
    monkeypatch.setattr(cli, 'ASSETS', ('docker-compose.yml',))
    monkeypatch.setattr(cli, 'files_for_context', lambda _: [Path('docker-compose.yml'), Path('app.py')])
    monkeypatch.setattr(cli, 'check', lambda _: {
        'dirty': False, 'platform': 'linux/amd64', 'commit': 'a' * 40})
    monkeypatch.setattr(cli, 'base_image', lambda *a: 'python@sha256:pinned')
    monkeypatch.setattr(cli, 'inspect_image', lambda *a: {'Id': 'sha256:built'})
    calls = []
    def run(cmd, **kw):
        if cmd[:3] == ['git', 'rev-parse', 'HEAD']:
            return 'a' * 40
        if cmd[0] == 'git':
            return ''
        calls.append(cmd)
    monkeypatch.setattr(cli, 'run', run)
    out = tmp_path / 'candidate'
    cli.build(argparse.Namespace(output=out, development=False, role=['gateway'],
        version='2.0', python_base='python:3.12', node_base='node:22'))
    manifest = json.loads((out / 'release.json').read_text())
    assert manifest['images']['gateway']['id'] == 'sha256:built'
    assert manifest['inputs']['app.py'] == core.digest(root / 'app.py')
    assert 'PYTHON_BASE=python@sha256:pinned' in calls[0]
    assert '--load' in calls[0] and '--push' not in calls[0]


def test_verify_does_not_claim_business_acceptance(monkeypatch, tmp_path):
    manifest = candidate()
    manifest.pop('verification')
    monkeypatch.setattr(cli, 'load_manifest', lambda _: manifest)
    monkeypatch.setattr(cli, 'audit_archive', lambda _: {'layers': 3})
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        return '{}' if cmd[:2] == ['docker', 'run'] else ''
    monkeypatch.setattr(cli, 'run', run)
    path = tmp_path / 'release.json'
    cli.verify(argparse.Namespace(manifest=path, acceptance_report=None))
    result = json.loads(path.read_text())
    assert result['verification']['scope'] == 'packaging-only'
    runtime_smoke = next(cmd for cmd in calls
        if cmd[:2] == ['docker', 'run'] and '/worker-smoke.py' in cmd)
    assert runtime_smoke[runtime_smoke.index('--network') + 1] == 'none'
    assert 'SYS_ADMIN' in runtime_smoke and 'seccomp=unconfined' in runtime_smoke
    with pytest.raises(ValueError):
        core.require_delivery(result)
    assert all('--network' in cmd and 'none' in cmd for cmd in calls if cmd[:2] == ['docker', 'run'])


def test_offline_checksum_list_cannot_escape_bundle(tmp_path):
    (tmp_path / 'SHA256SUMS').write_text('bad  ../outside\n')
    with pytest.raises(ValueError, match='checksum/path'):
        offline.validate_bundle(tmp_path)


def test_run_persists_failed_build_log(tmp_path):
    log = tmp_path / 'build.log'
    with pytest.raises(ValueError, match='build-failed'):
        core.run([sys.executable, '-c', 'print("build-failed"); raise SystemExit(2)'], log=log)
    assert 'build-failed' in log.read_text()


def test_inspect_image_rejects_wrong_platform(monkeypatch):
    monkeypatch.setattr(core, 'run', lambda _: json.dumps([{
        'Id': 'sha256:test', 'Os': 'linux', 'Architecture': 'arm64'}]))
    with pytest.raises(ValueError, match='architecture mismatch'):
        core.inspect_image('test', 'linux/amd64')


def test_sbom_distinguishes_ecosystems_and_deduplicates_packages():
    manifest = candidate()
    package = {'name': 'example', 'version': '1.0'}
    manifest['verification']['packaging'] = {
        role: {'smoke': {'packages': [package]}} for role in core.ROLES}
    bom = core.application_sbom(manifest)
    assert bom['bomFormat'] == 'CycloneDX'
    assert {c['purl'] for c in bom['components']} == {'pkg:pypi/example@1.0', 'pkg:npm/example@1.0'}


@pytest.fixture
def documented_candidate(monkeypatch,tmp_path):
    """Exercise the real asset snapshot/hash pipeline without Docker I/O."""
    root=tmp_path/'repo'
    root.mkdir()
    for name in cli.ASSETS:
        path=root/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('services: {}' if name=='docker-compose.yml' else 'fixture asset\n')
    manual=root/'docs/configuration.md'
    manual.parent.mkdir(parents=True,exist_ok=True)
    manual.write_text('# Authoritative configuration for this source snapshot\n')
    stale=root/'deploy/configuration.md'
    stale.parent.mkdir(parents=True,exist_ok=True)
    stale.write_text('stale deployment copy must not ship\n')
    monkeypatch.setattr(cli,'ROOT',root)
    monkeypatch.setattr(cli,'check',lambda _: {
        'dirty':False,'platform':'linux/amd64','commit':'a'*40})
    monkeypatch.setattr(cli,'base_image',lambda *a:'base@sha256:pinned')
    monkeypatch.setattr(cli,'inspect_image',lambda ref,*_: {
        'Id':'sha256:'+ref,'RepoDigests':[ref.rsplit(':',1)[0]+'@sha256:pinned']})
    calls=[]
    def run(cmd,**kwargs):
        if cmd[:3]==['git','rev-parse','HEAD']:
            return 'a'*40
        if cmd[0]=='git':
            return ''
        calls.append(cmd)
        if cmd[:2]==['docker','save']:
            Path(cmd[3]).write_bytes(b'fixture image archive')
    monkeypatch.setattr(cli,'run',run)
    out=tmp_path/'candidate'
    cli.build(argparse.Namespace(output=out,development=False,role=None,
        version='2.0',python_base='python:3.12',node_base='node:22'))
    manifest_path=out/'release.json'
    manifest=json.loads(manifest_path.read_text())
    ids={role:item['id'] for role,item in manifest['images'].items()}
    verification=candidate()['verification']
    verification['images']=ids
    verification['acceptance']['images']=ids
    manifest['verification']=verification
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(cli,'load_manifest',lambda path:json.loads(path.read_text()))
    calls.clear()
    return root,out,manifest,calls


def test_configuration_manual_is_snapshot_bound_in_candidate_and_offline_bundle(documented_candidate,tmp_path):
    root,out,manifest,calls=documented_candidate
    manual='docs/configuration.md'
    expected=(root/manual).read_bytes()
    assert (out/'assets'/manual).read_bytes()==expected
    assert manifest['inputs'][manual]==core.digest(out/'assets'/manual)
    (root/manual).write_text('Later working tree configuration must not change this release\n')
    bundle=tmp_path/'bundle'
    cli.bundle(argparse.Namespace(manifest=out/'release.json',output=bundle))
    assert (out/'assets'/manual).read_bytes()==expected
    assert (bundle/manual).read_bytes()==expected
    assert not (bundle/'deploy/configuration.md').exists()
    assert f'{core.digest(bundle/manual)}  {manual}\n' in (bundle/'SHA256SUMS').read_text()
    readme=(bundle/'README.txt').read_text()
    assert manual in readme
    assert 'cross-links' in readme and 'source repository' in readme


@pytest.mark.parametrize('action',['bundle','publish'])
def test_modified_candidate_manual_blocks_delivery_before_docker_mutation(documented_candidate,tmp_path,action):
    _,out,_,calls=documented_candidate
    manual=out/'assets/docs/configuration.md'
    manual.parent.mkdir(parents=True,exist_ok=True)
    manual.write_text('tampered candidate configuration\n')
    args=argparse.Namespace(manifest=out/'release.json',output=tmp_path/'bundle',registry='registry.example/team')
    with pytest.raises(ValueError,match=r'docs/configuration\.md'):
        getattr(cli,action)(args)
    assert not calls
    assert not args.output.exists()
    assert not (out/'published.json').exists()


def test_bilingual_configuration_guides_are_in_offline_bundle(documented_candidate, tmp_path):
    root, out, manifest, calls = documented_candidate
    bundle = tmp_path / 'bilingual-bundle'
    cli.bundle(argparse.Namespace(manifest=out / 'release.json', output=bundle))
    for language in ('en', 'zh-CN'):
        for page in cli.DOCUMENTATION_PAGES:
            relative = f'docs/{language}/{page}'
            assert (bundle / relative).read_bytes() == (root / relative).read_bytes()
            assert manifest['inputs'][relative] == core.digest(out / 'assets' / relative)


def test_sbom_preserves_license_expression_and_unknown_metadata():
    manifest = {'version': '1.0', 'platform': 'linux/amd64', 'verification': {
        'packaging': {'gateway': {'smoke': {'packages': [
            {'name': 'known', 'version': '1', 'license_expression': 'Apache-2.0'},
            {'name': 'unknown', 'version': '1'},
        ]}}}}}
    components = {p['name']: p for p in core.application_sbom(manifest)['components']}
    assert components['known']['licenses'] == [{'expression': 'Apache-2.0'}]
    assert 'licenses' not in components['unknown']
