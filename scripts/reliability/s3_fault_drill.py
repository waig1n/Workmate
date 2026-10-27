"""Actual private RustFS outage; component evidence, not full HTTP acceptance."""
import argparse
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace
import uuid

from .cold_backup import docker, local_daemon
from .cleanup import source_hashes


def execute(directory):
    from workmate_product.artifact_storage import artifact_store_from_settings
    from workmate_product.application.artifact_objects import ArtifactObjectUnavailable
    from workmate_product.config.artifact_storage import ArtifactStoragePolicy
    from workmate_runtime.delivery.workspace_checkpoint import WorkspaceCheckpoint
    from workmate_runtime.delivery.files import DeliveryFiles

    local_daemon()
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    owner = 'fault-s3-' + uuid.uuid4().hex[:12]
    label = 'workmate.validation.owner=' + owner
    image = docker('image', 'inspect', 'rustfs/rustfs:1.0.0-rc.6', '--format', '{{.Id}}')
    before = set(docker('ps', '-aq', '--no-trunc').split())
    report = dict(passed=False, owner=owner, image=image, cases=[],
                  scope='real S3 publication/download/checkpoint components; no HTTP metadata claim')
    plan = dict(timeout_seconds=1.0, outage_operation_deadline_seconds=15,
                recovery_deadline_seconds=30, real_model_calls=0,
                injection='pause only exact owned container; finally unpause/remove')
    (directory / 'plan.json').write_text(json.dumps(plan, indent=2))
    (directory / 'source.json').write_text(json.dumps(source_hashes(Path.cwd()), indent=2))
    store = None
    created = False
    paused = False

    def owned():
        labels = json.loads(docker('inspect', owner, '--format', '{{json .Config.Labels}}'))
        if labels.get('workmate.validation.owner') != owner:
            raise RuntimeError('S3 fault target ownership mismatch')

    def outage(name, action):
        nonlocal paused
        owned()
        docker('pause', owner)
        paused = True
        started = time.monotonic()
        failure = None
        try:
            try:
                action()
            except ArtifactObjectUnavailable:
                detected = time.monotonic() - started
                if detected >= plan['outage_operation_deadline_seconds']:
                    failure = 'detection deadline exceeded'
            except Exception as error:
                detected = time.monotonic() - started
                failure = type(error).__name__ + ': ' + str(error)
            else:
                detected = time.monotonic() - started
                failure = 'Unavailable S3 operation returned success'
        finally:
            owned()
            docker('unpause', owner)
            paused = False
        ended = time.monotonic()
        deadline = ended + plan['recovery_deadline_seconds']
        while True:
            try:
                store.client.head_bucket(Bucket=store.bucket)
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.1)
        report['cases'].append(dict(name=name, passed=failure is None, error=failure, detection_seconds=detected,
            outage_seconds=ended-started, recovery_seconds=time.monotonic()-ended,
            manual_recovery='unpause dedicated S3'))

    try:
        docker('run', '-d', '--pull=never', '--name', owner, '--label', label,
               '--memory', '1g', '--memory-swap', '1g', '--cpus', '1', '--pids-limit', '256',
               '--tmpfs', '/data:rw,size=256m,mode=1777', '-p', '127.0.0.1::9000',
               '-e', 'RUSTFS_ACCESS_KEY=fault-fixture-access',
               '-e', 'RUSTFS_SECRET_KEY=fault-fixture-secret-only',
               '-e', 'RUSTFS_CONSOLE_ENABLE=false', image)
        created = True
        port = docker('port', owner, '9000/tcp').rsplit(':', 1)[1]
        store = artifact_store_from_settings(SimpleNamespace(
            artifact_storage=ArtifactStoragePolicy(backend='s3', timeout_seconds=1.0),
            artifact_s3_endpoint='http://127.0.0.1:' + port, artifact_s3_bucket=owner,
            artifact_s3_access_key='fault-fixture-access', artifact_s3_secret_key='fault-fixture-secret-only'))
        deadline = time.monotonic() + 45
        while True:
            try:
                store.client.create_bucket(Bucket=owner)
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.25)
        content = b'FAULT durable bytes\n'
        digest = hashlib.sha256(content).hexdigest()
        key = '.objects/sha256/' + digest[:2] + '/' + digest
        store.publish(key, content, digest)
        root = directory / 'workspace'
        (root / 'working').mkdir(parents=True)
        (root / 'working' / 'durable.txt').write_bytes(content)
        checkpoint = WorkspaceCheckpoint(store, max_files=10, max_bytes=4096)
        manifest = checkpoint.capture(root)
        (root / 'output').mkdir()
        delivery = DeliveryFiles(root, directory / 'artifact-local', max_file_bytes=4096,
                                 max_files=10, object_store=store)
        baseline = delivery.baseline()
        (root / 'output/report.txt').write_bytes(b'FAULT new deliverable')
        outage('download', lambda: store.snapshot(key, digest, len(content)))
        upload_key = '.inputs/fault/task/failed.txt'
        outage('upload', lambda: store.publish(upload_key, b'cannot publish'))
        # A timed-out PUT may finish after unpause; that orphan is permissible.
        # Failure returned no successful publication receipt or manifest.
        (root / 'working' / 'new.txt').write_bytes(b'new checkpoint bytes')
        outage('checkpoint_capture', lambda: checkpoint.capture(root))
        outage('artifact_seal_capture', lambda: delivery.capture(baseline))
        report['local_fallback_files'] = [str(p.relative_to(directory))
            for p in (directory / 'artifact-local').rglob('*') if p.is_file()]
        assert report['local_fallback_files'] == []
        snapshot = store.snapshot(key, digest, len(content))
        try:
            assert snapshot.path.read_bytes() == content
        finally:
            snapshot.close()
        checkpoint.restore(manifest, directory / 'restored')
        assert (directory / 'restored/working/durable.txt').read_bytes() == content
        assert not (directory / 'restored/working/new.txt').exists()
        report['passed'] = all(case['passed'] for case in report['cases'])
    except BaseException as error:
        report['error'] = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        cleanup_errors = []
        try:
            if created:
                owned()
                if paused:
                    docker('unpause', owner)
                try:
                    (directory / 's3.log').write_text(docker('logs', owner))
                except Exception as error:
                    cleanup_errors.append('logs: ' + type(error).__name__)
                docker('rm', '-f', '-v', owner)
            report['remaining'] = docker('ps', '-aq', '--filter', 'label=' + label).split()
            report['existing_preserved'] = before.issubset(set(docker('ps', '-aq', '--no-trunc').split()))
            report['passed'] &= not report['remaining'] and report['existing_preserved']
        except Exception as error:
            cleanup_errors.append(type(error).__name__ + ': ' + str(error))
        finally:
            if store:
                try:
                    store.client.close()
                except Exception as error:
                    cleanup_errors.append('client close: ' + type(error).__name__)
            report['cleanup_errors'] = cleanup_errors
            report['passed'] &= not cleanup_errors
            (directory / 'result.json').write_text(json.dumps(report, indent=2))
    if not report['passed']:
        raise RuntimeError('S3 drill failed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if args.run:
        execute(args.output)
    else:
        print('Prepared: isolated RustFS pause, publication/download/checkpoint and cleanup')
