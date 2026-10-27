"""Fail closed before starting any application role in a zero-cost fixture."""
import json
import os
from pathlib import Path
import socket


def verify_isolation(environment, connect=socket.create_connection, read=None):
    read = read or (lambda path: Path(path).read_text().strip())
    if environment.get('WORKMATE_LOAD_MODEL_KEY') != 'local-fixture-only':
        raise RuntimeError('Missing explicit fixture model credential')
    suspect = [name for name,value in environment.items() if value and name != 'WORKMATE_LOAD_MODEL_KEY'
        and (name.endswith(('API_KEY','ACCESS_KEY','SECRET_KEY','ACCESS_KEY_ID','SECRET_ACCESS_KEY'))
             or name.startswith(('LLM_', 'EMBEDDING_')))]
    if suspect:
        raise RuntimeError('Unexpected external model/storage credentials; values withheld')
    version=2
    try:
        limits = {name: read('/sys/fs/cgroup/'+name) for name in ('memory.max','pids.max','cpu.max')}
    except FileNotFoundError:
        version=1
        limits={'memory.max':read('/sys/fs/cgroup/memory/memory.limit_in_bytes'),
                'pids.max':read('/sys/fs/cgroup/pids/pids.max'),
                'cpu.max':read('/sys/fs/cgroup/cpu/cpu.cfs_quota_us')+' '+read('/sys/fs/cgroup/cpu/cpu.cfs_period_us')}
    for name,value in limits.items():
        first=value.split()[0]
        if first=='max' or int(first)<=0:
            raise RuntimeError('Unbounded workload cgroup: '+name)
        if name=='memory.max' and int(first)>=2**60:
            raise RuntimeError('Unbounded workload memory cgroup')
    for address in ('1.1.1.1','8.8.8.8'):
        try:
            connection=connect((address,443),timeout=2)
        except OSError:
            continue
        connection.close()
        raise RuntimeError('Unexpected public TCP egress')
    return {'passed':True,'public_tcp_blocked':True,'credential_check':'fixture_only',
            'cgroup_limits':limits,'cgroup_version':version,'scope':'application container; two public IPv4 probes, not exhaustive network policy verification'}


def record_isolation():
    report=verify_isolation(os.environ)
    Path('/evidence/isolation.json').write_text(json.dumps(report,indent=2))
