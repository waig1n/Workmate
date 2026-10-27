"""Release boundaries independent of a running Docker daemon."""
import io
import json
from pathlib import Path
import sys
import tarfile
import zipfile
import os
import subprocess
import shutil

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release.policy import classify, normalize_platform
from release.audit import audit_archive
from release.core import require_acceptance, release_compose
from release.core import require_delivery, CHECKS
from release.__main__ import files_for_context, main


def test_platform_normalization_and_rejection():
    assert normalize_platform('linux/x86_64') == 'linux/amd64'
    assert normalize_platform('linux/aarch64') == 'linux/arm64'
    for value in ['windows/amd64', 'linux/armv7', 'linux/unknown']:
        with pytest.raises(ValueError):
            normalize_platform(value)


def archive(path, entries):
    with tarfile.open(path, 'w') as target:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            target.addfile(info, io.BytesIO(data))


def test_audit_checks_environment_in_every_layer(tmp_path):
    layer = tmp_path / 'layer.tar'
    archive(layer, {'usr/local/lib/python3.12/site-packages/app/.env': b'EXAMPLE=value'})
    saved = tmp_path / 'image.tar'
    archive(saved, {'manifest.json': json.dumps([{'Layers': ['old/layer.tar']}]).encode(),
                    'old/layer.tar': layer.read_bytes()})
    with pytest.raises(ValueError, match=r'\.env'):
        audit_archive(saved)


def test_audit_allows_third_party_and_declared_resources(tmp_path):
    layer = tmp_path / 'layer.tar'
    archive(layer, {'usr/local/lib/python3.12/site-packages/agentscope/core.py': b'pass',
                    'usr/local/lib/python3.12/site-packages/workmate_runtime/delivery/files.py': b'pass'})
    saved = tmp_path / 'image.tar'
    archive(saved, {'manifest.json': json.dumps([{'Layers': ['layer.tar']}]).encode(),
                    'layer.tar': layer.read_bytes()})
    assert audit_archive(saved)['layers'] == 1


def test_audit_finds_environment_in_nested_wheel(tmp_path):
    wheel = io.BytesIO()
    with zipfile.ZipFile(wheel, 'w') as zipped:
        zipped.writestr('workmate_product/.env', 'EXAMPLE=value')
    layer = tmp_path / 'layer.tar'
    archive(layer, {'tmp/forgotten.whl': wheel.getvalue()})
    saved = tmp_path / 'image.tar'
    archive(saved, {'manifest.json': json.dumps([{'Layers': ['layer.tar']}]).encode(),
                    'layer.tar': layer.read_bytes()})
    with pytest.raises(ValueError, match=r'\.env'):
        audit_archive(saved)


def test_acceptance_is_bound_to_images_and_platform():
    manifest = {'platform': 'linux/amd64', 'images': {'gateway': {'id': 'sha256:a'}}}
    report = {'platform': 'linux/arm64', 'images': {'gateway': 'sha256:a'}, 'checks': {}}
    with pytest.raises(ValueError):
        require_acceptance(manifest, report)


def test_release_compose_removes_build_and_preserves_variables():
    source = {'name': 'workmate', 'services': {
        'gateway': {'build': {'context': '.'}, 'image': 'old',
                        'environment': {'DATABASE_URL': '${DATABASE_URL}'},
                        'volumes': ['./config/workmate.yaml:/app/config/workmate.yaml:ro']},
        'postgres': {'image': 'postgres:18-alpine'}}}
    result = release_compose(source, {'gateway': 'workmate-gateway:2.0'})
    assert 'build' not in result['services']['gateway']
    assert 'build' in source['services']['gateway']
    assert result['services']['gateway']['environment']['DATABASE_URL'] == '${DATABASE_URL}'
    assert result['services']['gateway']['image'] == 'workmate-gateway:2.0'


def test_context_excludes_credentials_and_host_native_dependencies(tmp_path):
    paths = ['backend/app/main.py', 'backend/.env', 'backend/.venv/secret.py',
             'frontend/node_modules/native.so', 'runtime/workmate_runtime/file.pyc',
             'backend/output/customer-report.html', 'backend/nested/data/input.csv',
             'frontend/coverage/index.html', 'frontend/test-results/trace.zip',
             'frontend/playwright-report/index.html', 'runtime/working/input.txt',
             'backend/.codex/session.json', 'runtime/dist/package.whl',
             'backend/local.sqlite', 'backend/local.sqlite3', 'backend/local.db']
    for name in paths:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('test')
    names = {str(p) for p in files_for_context(tmp_path)}
    assert 'backend/app/main.py' in names
    assert not names.intersection(paths[1:])


def test_context_rejects_symlink(tmp_path):
    directory = tmp_path / 'backend'
    directory.mkdir()
    (directory / 'leak').symlink_to('/etc/passwd')
    with pytest.raises(ValueError, match='Symlink'):
        list(files_for_context(tmp_path))


def test_delivery_requires_business_acceptance_and_clean_input():
    manifest = {'platform': 'linux/amd64', 'images': {
        role: {'id': 'sha256:' + role} for role in ['gateway', 'runtime', 'frontend']}}
    ids = {k: v['id'] for k, v in manifest['images'].items()}
    manifest['verification'] = {'images': ids}
    with pytest.raises(ValueError):
        require_delivery(manifest)
    manifest['verification']['acceptance'] = {'platform': manifest['platform'], 'images': ids,
        'checks': {check: 'passed' for check in CHECKS}, 'evidence': 'ci/run/42'}
    require_delivery(manifest)
    manifest['development'] = True
    with pytest.raises(ValueError, match='Development'):
        require_delivery(manifest)


def test_acceptance_rejects_changed_image():
    manifest = {'platform': 'linux/amd64', 'images': {'gateway': {'id': 'new'}}}
    with pytest.raises(ValueError, match='exact image'):
        require_acceptance(manifest, {'platform': 'linux/amd64', 'images': {'gateway': 'old'}})


def test_cli_rejects_invalid_version_before_docker(tmp_path):
    assert main(['build', '--version', '../../bad', '--output', str(tmp_path)]) == 1


@pytest.mark.parametrize('role,module', [
    ('gateway', 'scripts.apply_builtin_defaults'),
    ('runtime', 'workmate_runtime.operations.upgrade_check'),
])
def test_image_smoke_checks_operational_module_help(monkeypatch, capsys, role, module):
    from release import smoke

    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout='usage: private-help\n', stderr='')

    monkeypatch.setattr(subprocess, 'run', run)
    smoke.check_operational_cli(role)
    assert calls == [([sys.executable, '-m', module, '--help'], {
        'check': True, 'capture_output': True, 'text': True, 'timeout': 30,
    })]
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('failure', [
    subprocess.CalledProcessError(1, ['python', '-m', 'entry']),
    subprocess.TimeoutExpired(['python', '-m', 'entry'], 30),
])
def test_image_smoke_operational_cli_failure_blocks_verification(monkeypatch, failure):
    from release import smoke

    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(type(failure)):
        smoke.check_operational_cli('runtime')


@pytest.mark.parametrize('role', ['gateway', 'runtime'])
def test_image_smoke_invokes_operational_cli_before_success(monkeypatch, role):
    from types import SimpleNamespace
    from release import smoke

    monkeypatch.setattr(smoke, 'Path', lambda _: SimpleNamespace(
        read_text=lambda: json.dumps({'files': []})))
    monkeypatch.setattr(smoke, 'check_orm_defaults', lambda: None)
    calls = []

    def invalid_entrypoint(value):
        calls.append(value)
        raise RuntimeError('operational entrypoint unavailable')

    monkeypatch.setattr(smoke, 'check_operational_cli', invalid_entrypoint)
    with pytest.raises(RuntimeError, match='operational entrypoint unavailable'):
        smoke.main(role)
    assert calls == [role]


SCRIPT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('role', ['gateway', 'runtime'])
def test_image_smoke_orm_failure_blocks_both_roles(monkeypatch, role):
    from types import SimpleNamespace
    from release import smoke

    monkeypatch.setattr(smoke, 'Path', lambda _: SimpleNamespace(
        read_text=lambda: json.dumps({'files': []})))

    def invalid_defaults():
        raise RuntimeError('ORM context default failed')

    monkeypatch.setattr(smoke, 'check_orm_defaults', invalid_defaults)
    with pytest.raises(RuntimeError, match='ORM context default failed'):
        smoke.main(role)

@pytest.mark.parametrize('module',['compact_tool_transport','audit_tool_content_objects'])
def test_retention_cli_source_module_entry(tmp_path,module):
    """The deployed python -m CLI runs from shipped source."""
    assert classify(f'scripts/{module}.py')=='source'
    root=Path(__file__).resolve().parents[2]
    package=tmp_path/'scripts';package.mkdir()
    for name in ['__init__',module]:
        source=package/(name+'.py')
        shutil.copyfile(root/'backend/scripts'/(name+'.py'),source)
    environment={**os.environ,'PYTHONPATH':os.pathsep.join(str(root/p) for p in
        ['backend','packages/product/src','packages/contracts/src'])}
    result=subprocess.run([sys.executable,'-m',f'scripts.{module}','--help'],cwd=tmp_path,
        env=environment,capture_output=True,text=True,check=True,timeout=30)
    assert '--apply' in result.stdout
    assert len(list(package.glob('*.py'))) == 2


def test_application_sources_are_preserved():
    for name in ('app/main.py', 'workmate_runtime/roles/worker.py',
                 'workmate_product/persistence/models.py', 'workmate_contracts/__init__.py'):
        assert classify(name) == 'source'
    assert classify('workmate_product/skills/builtin_skills/demo/scripts/run.py') == 'resource'


def test_inventory_preserves_importable_source_and_wheel_records(tmp_path):
    from release.inventory import inventory
    (tmp_path / 'app').mkdir()
    source = tmp_path / 'app/__init__.py'
    source.write_text('def add(a, b): return a + b\n')
    record = tmp_path / 'app.dist-info/RECORD'
    record.parent.mkdir()
    record.write_text('app/__init__.py,,\n')
    before = {p: p.read_bytes() for p in (source, record)}
    report = tmp_path / 'sources.json'
    inventory(tmp_path, report)
    assert all(p.read_bytes() == data for p, data in before.items())
    assert json.loads(report.read_text())['files'][0]['path'] == 'app/__init__.py'
    subprocess.run([sys.executable, '-c', 'import app; assert app.add(2, 3) == 5'],
                   cwd=tmp_path, check=True)


def test_inventory_rejects_empty_installation(tmp_path):
    from release.inventory import inventory
    with pytest.raises(ValueError, match='No installed application sources'):
        inventory(tmp_path, tmp_path / 'sources.json')


def test_audit_allows_application_source_in_wheel(tmp_path):
    wheel = io.BytesIO()
    with zipfile.ZipFile(wheel, 'w') as zipped:
        zipped.writestr('workmate_product/main.py', 'pass')
    layer = tmp_path / 'layer.tar'
    archive(layer, {'tmp/application.whl': wheel.getvalue()})
    saved = tmp_path / 'image.tar'
    archive(saved, {'manifest.json': json.dumps([{'Layers': ['layer.tar']}]).encode(),
                    'layer.tar': layer.read_bytes()})
    assert audit_archive(saved)['layers'] == 1


@pytest.mark.parametrize('name', ['backend/.netrc', 'backend/.aws/credentials', 'runtime/id_rsa'])
def test_context_rejects_private_credential_files(tmp_path, name):
    target = tmp_path / name
    target.parent.mkdir(parents=True)
    target.write_text('fixture')
    with pytest.raises(ValueError, match='Private credential'):
        list(files_for_context(tmp_path))


def test_audit_detects_private_key_under_arbitrary_filename(tmp_path):
    layer = tmp_path / 'layer.tar'
    archive(layer, {'app/data.txt': b'-----BEGIN PRIVATE KEY-----\nfixture'})
    saved = tmp_path / 'image.tar'
    archive(saved, {'manifest.json': json.dumps([{'Layers': ['layer.tar']}]).encode(),
                    'layer.tar': layer.read_bytes()})
    with pytest.raises(ValueError, match='Private key material'):
        audit_archive(saved)


def test_frontend_notices_keep_scoped_package_paths(tmp_path):
    source = tmp_path / 'modules'
    license_file = source / '@example/library/LICENSE.md'
    license_file.parent.mkdir(parents=True)
    license_file.write_text('fixture copyright and permission')
    (license_file.parent / 'index.js').write_text('module.exports = {};')
    (license_file.parent / 'LICENSES').mkdir()
    (license_file.parent / 'LICENSES/MIT.txt').write_text('fixture MIT terms')
    (source / 'outside').symlink_to(tmp_path, target_is_directory=True)
    output = tmp_path / 'notices'
    subprocess.run(['node', str(SCRIPT_ROOT / 'scripts/release/frontend-licenses.cjs'),
                    str(source), str(output)], check=True)
    assert (output / '@example/library/LICENSE.md').read_text() == license_file.read_text()
    assert (output / '@example/library/LICENSES/MIT.txt').read_text() == 'fixture MIT terms'
    assert not (output / '@example/library/index.js').exists()
    assert not (output / 'outside').exists()
