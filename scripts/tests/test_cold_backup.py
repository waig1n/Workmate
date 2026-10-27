"""Cold backup refuses incomplete or unsafe manifests before Docker mutations."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.reliability.cold_backup import verify_bundle, validate_inventory


def inventory():
    return {'project': 'dr-source', 'volumes': ['dr-source_pg'], 'binds': [],
            'external_volumes': [], 'services': {'postgres': {'image': 'sha256:123'}}}


def test_inventory_rejects_missing_data_and_external_storage():
    for field, value in [('volumes', []), ('external_volumes', ['shared']),
                         ('binds', ['/data/business'])]:
        value_inventory = {**inventory(), field: value}
        with pytest.raises(ValueError):
            validate_inventory(value_inventory)
    validate_inventory(inventory())


def bundle(tmp_path):
    payload = b'cold-volume'
    (tmp_path / 'volume-0.tar').write_bytes(payload)
    manifest = {'format': 1, 'inventory': inventory(), 'files': {
        'volume-0.tar': {'volume': 'dr-source_pg', 'size': len(payload),
                         'sha256': hashlib.sha256(payload).hexdigest()}}}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    return manifest


def test_verified_bundle(tmp_path):
    manifest = bundle(tmp_path)
    assert verify_bundle(tmp_path) == manifest


@pytest.mark.parametrize('fault', ['corrupt', 'missing', 'traversal', 'symlink', 'incomplete'])
def test_bundle_fails_closed(tmp_path, fault):
    manifest = bundle(tmp_path)
    if fault == 'corrupt':
        (tmp_path / 'volume-0.tar').write_bytes(b'corrupt')
    elif fault == 'missing':
        (tmp_path / 'volume-0.tar').unlink()
    elif fault == 'traversal':
        manifest['files']['../outside.tar'] = manifest['files'].pop('volume-0.tar')
    elif fault == 'symlink':
        original = tmp_path / 'volume-0.tar'
        original.rename(tmp_path / 'outside.tar')
        original.symlink_to(tmp_path / 'outside.tar')
    else:
        manifest['files'] = {}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        verify_bundle(tmp_path)


def test_rejects_remote_driver_and_undeclared_actual_mount(monkeypatch):
    from scripts.reliability import cold_backup as module
    volume = {'Driver': 'local', 'Options': {'type': 'nfs'},
              'Labels': {'com.docker.compose.project': 'dr-source'}}
    container = {'Name': '/stopped', 'Image': 'sha256:deployed',
                 'Config': {'Labels': {'com.docker.compose.project': 'dr-source',
                                       'com.docker.compose.service': 'postgres'}},
                 'State': {'Running': False}, 'Mounts': []}
    def fake(*args):
        if args[:2] == ('ps', '-aq'): return 'c'
        if args[0] == 'inspect': return json.dumps([container])
        return json.dumps([volume])
    monkeypatch.setattr(module, 'docker', fake)
    with pytest.raises(ValueError, match='local'):
        module.assert_quiet(inventory())
    volume['Options'] = None
    container['Mounts'] = [{'Type': 'volume', 'Name': 'not-listed', 'RW': True}]
    with pytest.raises(ValueError, match='mount'):
        module.assert_quiet(inventory())
    container['Mounts'] = [{'Type': 'volume', 'Name': 'dr-source_pg', 'RW': True}]
    current = inventory()
    module.assert_quiet(current)
    assert current['services']['postgres']['deployed_image_ids'] == ['sha256:deployed']


def test_backup_and_restore_fresh_targets(tmp_path, monkeypatch):
    from scripts.reliability import cold_backup as module
    calls=[]
    monkeypatch.setattr(module,'assert_quiet',lambda value: None)
    monkeypatch.setattr(module,'docker',lambda *args: calls.append(args) or '')
    def archive(image, volume, root, name, *, restore=False):
        if not restore: (root/name).write_bytes(volume.encode())
    monkeypatch.setattr(module,'archive_volume',archive)
    root=tmp_path/'backup'
    module.backup(inventory(),root,'sha256:helper')
    restored=module.restore(root,'sha256:helper')
    assert not restored['services_started']
    assert restored['volumes']['dr-source_pg'].startswith('workmate-restore-')
    assert (root/'volume-0.tar').stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError): module.backup(inventory(),root,'helper')
    # Invalid bundle rejected before creating a target volume.
    calls.clear()
    (root/'volume-0.tar').write_bytes(b'changed')
    with pytest.raises(ValueError): module.restore(root,'helper')
    assert not calls


def test_helper_preserves_ownership_and_isolation(tmp_path, monkeypatch):
    from scripts.reliability import cold_backup as module
    calls=[]
    monkeypatch.setattr(module,'docker',lambda *args: calls.append(args))
    module.archive_volume('image','volume',tmp_path,'volume-0.tar')
    assert 'none' in calls[0] and 'chown' in ' '.join(calls[0])
    module.archive_volume('image','target',tmp_path,'volume-0.tar',restore=True)
    assert any('dst=/backup,readonly' in x for x in calls[1])
    assert 'test -z' in ' '.join(calls[1])


def test_local_daemon_rejects_remote(monkeypatch):
    from scripts.reliability import cold_backup as module
    monkeypatch.setenv('DOCKER_HOST','ssh://example.test')
    with pytest.raises(ValueError): module.local_daemon()
    monkeypatch.delenv('DOCKER_HOST')
    monkeypatch.setattr(module,'docker',lambda *a: json.dumps([{'Endpoints':{'docker':{'Host':'unix:///var/run/docker.sock'}}}]))
    module.local_daemon()


def test_inventory_resolves_compose_and_rejects_writable_bind(monkeypatch,tmp_path):
    from scripts.reliability import cold_backup as module
    config={'volumes':{'pg':{'name':'dr-source_pg'}},'services':{'postgres':{'image':'pg','volumes':[]}}}
    monkeypatch.setattr(module,'docker',lambda *a: json.dumps(config))
    assert module.inventory([tmp_path/'compose.yml'],'dr-source')['volumes']==['dr-source_pg']
    config['services']['postgres']['volumes']=[{'type':'bind','source':'/host','read_only':False}]
    with pytest.raises(ValueError): module.inventory([tmp_path/'compose.yml'],'dr-source')


def test_cli_dry_run_and_explicit_apply(monkeypatch,tmp_path,capsys):
    import sys
    from scripts.reliability import cold_backup as module
    monkeypatch.setattr(module,'local_daemon',lambda:None)
    monkeypatch.setattr(module,'docker',lambda *a:'sha256:helper')
    monkeypatch.setattr(module,'inventory',lambda *a:inventory())
    monkeypatch.setattr(module,'assert_quiet',lambda *a:None)
    args=['cold_backup','backup','--bundle',str(tmp_path/'new'),'--project','dr-source','--compose-file','c.yml']
    monkeypatch.setattr(sys,'argv',args)
    module.main()
    assert '"applied": false' in capsys.readouterr().out
    monkeypatch.setattr(sys,'argv',args+['--apply'])
    with pytest.raises(SystemExit): module.main()
    bundle(tmp_path)
    monkeypatch.setattr(sys,'argv',['cold_backup','verify','--bundle',str(tmp_path)])
    module.main()
    monkeypatch.setattr(sys,'argv',['cold_backup','restore','--bundle',str(tmp_path)])
    module.main()


def test_explicit_context_takes_precedence_over_host(monkeypatch):
    from scripts.reliability import cold_backup as module
    monkeypatch.setenv('DOCKER_HOST','unix:///var/run/docker.sock')
    monkeypatch.setenv('DOCKER_CONTEXT','remote')
    def inspect(*args):
        assert args == ('context','inspect','remote')
        return json.dumps([{'Endpoints':{'docker':{'Host':'ssh://example.test'}}}])
    monkeypatch.setattr(module,'docker',inspect)
    with pytest.raises(ValueError): module.local_daemon()
