"""Bounded local source acceptance; dry-run does not contact Docker or services."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid

from scripts.reliability.cold_backup import local_daemon

ROOT = Path(__file__).resolve().parents[2]
RESOURCE_LIMITS = {
    'workload': {'memory_mib': 3072, 'cpus': 2, 'pids': 512},
    'postgres': {'memory_mib': 768, 'cpus': 0.5, 'pids': 128},
    'redis': {'memory_mib': 768, 'cpus': 0.25, 'pids': 64},
    'migration': {'memory_mib': 1024, 'cpus': 0.5, 'pids': 128},
}

def resource_args(role):
    limits = RESOURCE_LIMITS[role]
    return ['--memory', str(limits['memory_mib'])+'m',
            '--memory-swap', str(limits['memory_mib'])+'m',
            '--cpus', str(limits['cpus']), '--pids-limit', str(limits['pids'])]

PYTHONPATH = '/support:/source/scripts/reliability:/source/runtime:/source/packages/product/src:/source/packages/contracts/src:/source/backend:/source:/extra-gateway'


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-image', required=True, help='Existing dependency image; resolved to immutable ID')
    parser.add_argument('--gateway-image', required=True)
    parser.add_argument('--postgres-image', default='postgres:18')
    parser.add_argument('--redis-image', default='redis:7-alpine')
    parser.add_argument('--output', type=Path, default=Path('/tmp/workmate-reliability-load'))
    parser.add_argument('--workload', choices=('text', 'file', 'approval', 'skill', 'mcp', 'rag', 'mixed'), default='text')
    parser.add_argument('--mode', choices=('smoke', 'load'), default='smoke')
    parser.add_argument('--concurrency', type=int, choices=(1, 2, 4, 8), default=1)
    parser.add_argument('--tasks', type=int, choices=range(1, 101), default=8)
    parser.add_argument('--repeat', type=int, choices=range(1, 11), default=1)
    parser.add_argument('--duration-seconds', type=int, default=0,
                        help='Continuous same-process admission window, 1..259200; 0 uses repeat count')
    parser.add_argument('--arrival-rate', type=float, default=1,
                        help='Maximum task admissions/second; concurrency cap still applies')
    parser.add_argument('--cooldown-seconds',type=int,default=0,help='Keep roles alive after stop-admission, 0..1800 seconds')
    parser.add_argument('--burst-rate',type=float,default=0,help='Optional 10-minute peak every six hours')
    parser.add_argument('--reuse-conversations',action='store_true',help='Reuse half of batch conversations in continuous windows')
    parser.add_argument('--workers',type=int,choices=(1,2),default=2)
    parser.add_argument('--capacity-per-worker',type=int,choices=(2,4),default=2)
    parser.add_argument('--run', action='store_true')
    args=parser.parse_args(argv)
    if not 0<=args.duration_seconds<=259200 or not 0<args.arrival_rate<=100:
        parser.error('Invalid duration or arrival rate')
    if args.duration_seconds and args.repeat!=1:
        parser.error('Continuous window cannot combine repeated windows')
    if not 0<=args.cooldown_seconds<=1800 or (args.cooldown_seconds and not args.duration_seconds):
        parser.error('Cooldown requires a duration window, with 0..1800 seconds')
    if args.burst_rate and not args.arrival_rate<=args.burst_rate<=100:
        parser.error('Peak rate must be >= base rate and <=100')
    if (args.burst_rate or args.reuse_conversations) and not args.duration_seconds:
        parser.error('Peak and reuse require a continuous window')
    return args


def plan(args):
    return {'scope': 'current source, isolated independent processes, deterministic text model',
            'concurrency': args.concurrency, 'tasks': (20 if args.workload=='mixed' else 1) if args.mode == 'smoke' else args.tasks,
            'repeats': args.repeat, 'mode': args.mode, 'workload': args.workload, 'file_bytes': 1024 if args.mode=='smoke' else 1048576, 'workers': args.workers, 'capacity_per_worker': args.capacity_per_worker,
            'task_budget_seconds': 32, 'run': args.run, 'resource_limits': RESOURCE_LIMITS,
            'duration_seconds':args.duration_seconds,'arrival_rate':args.arrival_rate,
            'fixture_context_tokens':1048576,
            'cooldown_seconds':args.cooldown_seconds,
            'burst_rate':args.burst_rate,'reuse_conversations':args.reuse_conversations,
            'conversation_turn_limit':2,
            'workspace_lifecycle':{'max_warm_per_worker':2,'warm_ttl_seconds':30,'sweep_interval_seconds':2},
            'requires': 'existing local images; isolated Bubblewrap container uses SYS_ADMIN and unconfined seccomp/apparmor'}


def cleanup_owned(owner, containers, network_created, evidence):
    """Check exact ownership before deletion, then independently inventory leftovers."""
    report = {'failures': [], 'remaining_containers': [], 'remaining_networks': [],
              'remaining_volumes': [], 'captured_volumes': []}
    volumes = set()
    def call(parts):
        try:
            return subprocess.run(['docker', *parts], capture_output=True, text=True)
        except OSError as error:
            report['failures'].append({'operation': parts[:2], 'error': type(error).__name__})
            return None
    for name in reversed(containers):
        inspected = call(['container', 'inspect', name])
        if inspected is None or inspected.returncode:
            if inspected is not None and 'no such container' not in inspected.stderr.casefold() and 'no such object' not in inspected.stderr.casefold():
                report['failures'].append({'inspect_container': name})
            continue
        try:
            item = json.loads(inspected.stdout)[0]
            if item['Config']['Labels'].get('workmate.validation.owner') != owner:
                raise ValueError('ownership mismatch')
            volumes.update(m['Name'] for m in item.get('Mounts', []) if m['Type'] == 'volume')
        except (ValueError, KeyError, IndexError, TypeError):
            report['failures'].append({'unverified_container': name})
            continue
        logs = call(['logs', name])
        if logs is not None:
            (evidence / (name + '.docker.log')).write_text(logs.stdout + logs.stderr)
        removed = call(['rm', '-f', '-v', name])
        if removed is None or removed.returncode:
            report['failures'].append({'remove_container': name})
    if network_created:
        inspected = call(['network', 'inspect', owner, '--format', '{{json .Labels}}'])
        try:
            verified = inspected is not None and inspected.returncode == 0 and json.loads(inspected.stdout).get('workmate.validation.owner') == owner
        except (ValueError, AttributeError):
            verified = False
        if verified:
            removed = call(['network', 'rm', owner])
            if removed is None or removed.returncode:
                report['failures'].append({'remove_network': owner})
        else:
            report['failures'].append({'unverified_network': owner})
    for kind, key in (('container', 'remaining_containers'), ('network', 'remaining_networks'), ('volume', 'remaining_volumes')):
        parts = [kind, 'ls', '--filter', 'label=workmate.validation.owner=' + owner, '--format', '{{.ID}}' if kind != 'volume' else '{{.Name}}']
        if kind == 'container':
            parts.append('--all')
        inventory = call(parts)
        if inventory is None or inventory.returncode:
            report['failures'].append({'inventory': kind})
        else:
            report[key].extend(inventory.stdout.split())
    # Anonymous image volumes have no owner label. Capture them before rm -v and
    # inspect exact names afterwards; never delete arbitrary unlabeled volumes.
    for volume in sorted(volumes):
        inspected = call(['volume', 'inspect', volume])
        if inspected is None:
            continue
        if inspected.returncode == 0:
            report['remaining_volumes'].append(volume)
        elif 'no such volume' not in inspected.stderr.casefold():
            report['failures'].append({'inspect_volume': volume})
    report['captured_volumes'] = sorted(volumes)
    report['remaining_volumes'] = sorted(set(report['remaining_volumes']))
    if any(report[key] for key in ('remaining_containers', 'remaining_networks', 'remaining_volumes')):
        report['failures'].append({'remaining_resources': True})
    (evidence / 'cleanup.json').write_text(json.dumps(report, indent=2))
    return report


def execute(args):
    local_daemon()
    def command(*parts, **kwargs):
        return subprocess.run(parts, check=True, **kwargs)
    def image(ref):
        return subprocess.check_output(['docker', 'image', 'inspect', ref, '--format', '{{.Id}}'], text=True).strip()
    images = {key: image(getattr(args, key + '_image')) for key in ('runtime', 'gateway', 'postgres', 'redis')}
    owner = 'reliability-load-' + uuid.uuid4().hex[:12]
    directory = args.output.resolve() / owner
    evidence = directory / 'evidence'
    directory.mkdir(parents=True,mode=0o700)
    evidence.mkdir(mode=0o700)
    source = directory / 'source'
    # Copy only source/configuration; never mount .env, local credentials or business data.
    for relative in ('runtime/workmate_runtime', 'packages/product/src', 'packages/contracts/src',
                     'backend/app', 'backend/alembic', 'scripts/reliability', 'config'):
        shutil.copytree(ROOT / relative, source / relative, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copy2(ROOT / 'backend/alembic.ini', source / 'backend/alembic.ini')
    files = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in source.rglob('*') if p.is_file()}
    (evidence / 'source.json').write_text(json.dumps(files, indent=2))
    (evidence / 'plan.json').write_text(json.dumps({**plan(args), 'images': images, 'owner': owner}, indent=2))
    support = source / 'scripts/reliability/load_support'
    shared = directory / 'shared'
    extra = directory / 'extra-gateway'
    shared.mkdir(); extra.mkdir()
    containers = []
    network_created = False
    label = 'workmate.validation.owner=' + owner
    pg, redis = owner + '-pg', owner + '-redis'
    env = ['--env', 'DATABASE_URL=postgresql+asyncpg://fixture:fixture@' + pg + ':5432/fixture',
           '--env', 'WORKMATE_COORDINATION_REDIS_URL=redis://' + redis + ':6379/0',
           '--env', 'WORKMATE_LOAD_MODEL_KEY=local-fixture-only', '--env', 'WORKMATE_LOAD_NAMESPACE=' + owner, '--env', 'WORKMATE_CONFIG_FILE=',
           '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'PYTHONPATH=' + PYTHONPATH,
           '--env', 'WORKMATE_LOAD_WORKLOAD=' + args.workload, '--env', 'WORKMATE_LOAD_MODE=' + args.mode, '--env', 'WORKMATE_LOAD_CONCURRENCY=' + str(args.concurrency),
           '--env', 'WORKMATE_LOAD_REPEAT=' + str(args.repeat), '--env', 'LOAD_TASKS=' + str(args.tasks)]
    env += ['--env','WORKMATE_LOAD_DURATION='+str(args.duration_seconds),'--env','WORKMATE_LOAD_RATE='+str(args.arrival_rate)]
    env += ['--env','WORKMATE_LOAD_COOLDOWN='+str(args.cooldown_seconds)]
    env += ['--env','WORKMATE_LOAD_BURST_RATE='+str(args.burst_rate),'--env','WORKMATE_LOAD_REUSE='+('1' if args.reuse_conversations else '')]
    env += ['--env','WORKMATE_LOAD_WORKERS='+str(args.workers),'--env','WORKMATE_LOAD_WORKER_CAPACITY='+str(args.capacity_per_worker)]
    mounts = []
    for local, target, readonly in ((source, '/source', True), (support, '/support', True),
                                   (extra, '/extra-gateway', True), (evidence, '/evidence', False),
                                   (shared, '/shared', False)):
        mounts += ['--mount', f'type=bind,src={local},dst={target}' + (',readonly' if readonly else '')]
    def launch(name, *parts, **kwargs):
        containers.append(name)
        return command('docker', 'run', '--name', name, '--label', label, '--network', owner, *parts, **kwargs)
    result = None
    try:
        command('docker', 'network', 'create', '--internal', '--label', label, owner, stdout=subprocess.DEVNULL)
        network_created = True
        launch(pg, '-d', *resource_args('postgres'), '--env', 'POSTGRES_USER=fixture', '--env', 'POSTGRES_PASSWORD=fixture',
               '--env', 'POSTGRES_DB=fixture', images['postgres'], 'postgres', '-c',
               'shared_preload_libraries=pg_stat_statements', stdout=subprocess.DEVNULL)
        launch(redis, '-d', *resource_args('redis'), images['redis'], 'redis-server', '--save', '', '--appendonly', 'yes',
               '--appendfsync', 'everysec', '--maxmemory', '512mb', '--maxmemory-policy', 'noeviction', stdout=subprocess.DEVNULL)
        deadline = time.monotonic() + 45
        while subprocess.run(['docker', 'exec', pg, 'pg_isready', '-h', '127.0.0.1', '-U', 'fixture', '-d', 'fixture'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            if time.monotonic() > deadline:
                raise TimeoutError('isolated PostgreSQL startup')
            time.sleep(.25)
        copy = owner + '-copy'
        containers.append(copy)
        command('docker', 'create', '--name', copy, '--label', label, '--entrypoint', 'true', images['gateway'], stdout=subprocess.DEVNULL)
        command('docker', 'cp', copy + ':/usr/local/lib/python3.12/site-packages/.', str(extra))
        launch(owner + '-migration', *resource_args('migration'), *env, *mounts, '--workdir', '/source/backend', '--entrypoint', 'python',
               images['gateway'], '-m', 'alembic', '-c', '/source/backend/alembic.ini', 'upgrade', 'head')
        with (evidence / 'harness.log').open('w') as output:
            launch(owner + '-workload', '--init', *resource_args('workload'), *env, *mounts, '--cap-add', 'SYS_ADMIN', '--security-opt',
                   'seccomp=unconfined', '--security-opt', 'apparmor=unconfined', '--workdir', '/tmp',
                   '--entrypoint', 'python', images['runtime'], '/support/run.py',
                   stdout=output, stderr=subprocess.STDOUT, timeout=180 + args.duration_seconds + args.cooldown_seconds + args.repeat * args.tasks * 90)
        result = {'passed': True}
    finally:
        cleanup = cleanup_owned(owner, containers, network_created, evidence)
        if cleanup['failures']:
            raise RuntimeError('Owned resource cleanup failed; see cleanup.json')
    print(json.dumps({**result, 'evidence': str(evidence)}))


def main(argv=None):
    args = parse_args(argv)
    if not args.run:
        print(json.dumps(plan(args), indent=2))
        return
    execute(args)


if __name__ == '__main__':
    main()
