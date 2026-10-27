"""Fail-closed fixture safety samples, including sandbox descendants."""
import os
from pathlib import Path


def process_tree(root, proc=Path('/proc')):
    rows={}
    for directory in proc.iterdir():
        if not directory.name.isdigit():continue
        try:
            values=(directory/'stat').read_text().rsplit(')',1)[1].split()
            pid=int(directory.name)
            rows[pid]={'pid':pid,'ppid':int(values[1]),
                'cpu_seconds':(int(values[11])+int(values[12]))/os.sysconf('SC_CLK_TCK'),
                'rss_bytes':int(values[21])*os.sysconf('SC_PAGE_SIZE'),
                'fds':len(list((directory/'fd').iterdir()))}
        except FileNotFoundError:continue  # A child may exit during enumeration.
    selected={root}
    while True:
        following=selected|{pid for pid,row in rows.items() if row['ppid'] in selected}
        if following==selected:break
        selected=following
    if root not in rows:raise RuntimeError('Sampler root disappeared')
    return [rows[pid] for pid in sorted(selected) if pid in rows]


def safety_errors(row, initial_oom):
    errors=[]
    if row['available_bytes']<2*2**30:errors.append('host memory below 2GiB')
    if row['disk_free_bytes']<max(20*2**30,row['disk_total_bytes']*.1):errors.append('disk reserve breached')
    if row['oom_kill']>initial_oom:errors.append('OOM counter increased')
    if row['evidence_bytes']>=4*2**30:errors.append('fixture data budget reached')
    return errors


def sample(evidence=Path('/evidence'), shared=Path('/shared')):
    memory={line.split(':')[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines()}
    disk=os.statvfs(shared)
    events=dict(line.split() for line in Path('/sys/fs/cgroup/memory.events').read_text().splitlines())
    size=sum(p.stat().st_size for root in (evidence,shared) for p in root.rglob('*') if p.is_file())
    return {'available_bytes':memory['MemAvailable'],'disk_free_bytes':disk.f_bavail*disk.f_frsize,
            'disk_total_bytes':disk.f_blocks*disk.f_frsize,'oom_kill':int(events['oom_kill']),
            'evidence_bytes':size,'process_tree':process_tree(1),
            'cgroup_memory_bytes':int(Path('/sys/fs/cgroup/memory.current').read_text()),
            'cgroup_pids':int(Path('/sys/fs/cgroup/pids.current').read_text()),
            'cgroup_cpu':Path('/sys/fs/cgroup/cpu.stat').read_text()}
