"""Read-only CAPACITY PostgreSQL timing snapshot through a local SSH tunnel."""
import argparse
import asyncio
import ipaddress
import json
import math
from pathlib import Path
from urllib.parse import urlsplit

# Both timestamps use PG server defaults, not Worker wall time or local monotonic.
# created_at is transaction timestamp: these are durable-event interval proxies.
TASK_SQL="""SELECT t.id,t.status,t.created_at,
 min(e.created_at) FILTER (WHERE e.payload->>'type'='REPLY_START') AS first_reply_start,
 max(e.created_at) FILTER (WHERE e.payload->>'type'='REPLY_END') AS last_reply_end,
 count(*) FILTER (WHERE e.payload->>'type'='REPLY_START') AS start_count,
 count(*) FILTER (WHERE e.payload->>'type'='REPLY_END') AS end_count,
 extract(epoch FROM (min(e.created_at) FILTER (WHERE e.payload->>'type'='REPLY_START')-t.created_at))::float8 AS queue_seconds,
 CASE WHEN t.status IN ('completed','failed','cancelled','interrupted') THEN
 extract(epoch FROM (max(e.created_at) FILTER (WHERE e.payload->>'type'='REPLY_END')-
 min(e.created_at) FILTER (WHERE e.payload->>'type'='REPLY_START')))::float8 END AS execution_seconds
 FROM tasks t LEFT JOIN runtime_events e ON e.task_id=t.id
 WHERE t.id=ANY($1::varchar[]) GROUP BY t.id ORDER BY t.id"""
ACTIVITY_SQL="""SELECT state,wait_event_type,wait_event,count(*)::int AS count,
 max(extract(epoch FROM (clock_timestamp()-query_start)))::float8 AS max_query_age_seconds
 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
 GROUP BY state,wait_event_type,wait_event ORDER BY state,wait_event_type,wait_event"""
STATEMENTS_SQL="""SELECT sum(calls)::bigint AS calls,sum(total_exec_time)::float8 AS total_exec_time_ms,
 sum(rows)::bigint AS rows,sum(shared_blks_hit)::bigint AS shared_blks_hit,
 sum(shared_blks_read)::bigint AS shared_blks_read,sum(temp_blks_written)::bigint AS temp_blks_written
 FROM pg_stat_statements WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database())"""


def validate_dsn(dsn):
    parsed=urlsplit(dsn)
    if parsed.scheme not in ('postgres','postgresql') or parsed.query or parsed.fragment:
        raise ValueError('Only plain loopback PostgreSQL DSNs are accepted')
    try:valid=ipaddress.ip_address(parsed.hostname or '').is_loopback
    except ValueError:valid=parsed.hostname=='localhost'
    if not valid:raise ValueError('PostgreSQL must use a loopback SSH tunnel')
    return dsn


def task_rows(summary):return [row for run in summary.get('runs',[]) for row in run.get('tasks',[])]
def task_ids(summary):return sorted({row['id'] for row in task_rows(summary) if row.get('id')})
def p95(values):
    valid=sorted(float(v) for v in values if v is not None and math.isfinite(float(v)) and v>=0)
    return valid[max(0,math.ceil(.95*len(valid))-1)] if valid else None


def delivery(summary=None):
    rows=task_rows(summary or {});body_intervals=[];start_intervals=[];spreads=[];invalid=0;missing_readers=0
    def finite(value):return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)
    for row in rows:
        readers=row.get('reader_timings')
        if readers is None:
            streams=row.get('streams',{})
            readers=[{'run_started_monotonic':v.get('started'),'first_body_monotonic':v.get('first'),
                      'terminal_monotonic':v.get('terminal')} for v in (streams.values() if isinstance(streams,dict) else streams)]
        body=[];started=[];terminals=[]
        for reader in readers:
            first=reader.get('first_body_monotonic');start=reader.get('run_started_monotonic');end=reader.get('terminal_monotonic')
            if not finite(end) or (finite(first) and end<first) or (finite(start) and end<start):
                invalid+=1;continue
            terminals.append(end)
            if finite(first):body.append(end-first)
            else:missing_readers+=1
            if finite(start):started.append(end-start)
        # Frozen client aggregates remain usable when raw reader timings are absent.
        aggregate=row.get('first_body_to_terminal_seconds')
        if body:body_intervals.append(max(body))
        elif not readers and finite(aggregate) and aggregate>=0:body_intervals.append(aggregate)
        if started:start_intervals.append(max(started))
        spread=row.get('readers_terminal_spread_seconds')
        if terminals:spreads.append(max(terminals)-min(terminals))
        elif not readers and finite(spread) and spread>=0:spreads.append(spread)
    return {'p95_seconds':p95(body_intervals),'first_body_to_terminal_p95_seconds':p95(body_intervals),
            'run_started_to_terminal_p95_seconds':p95(start_intervals),'readers_terminal_spread_p95_seconds':p95(spreads),
            'sample_count':len(body_intervals),'missing_tasks':len(rows)-len(body_intervals),
            'run_started_sample_count':len(start_intervals),'run_started_missing_tasks':len(rows)-len(start_intervals),
            'spread_sample_count':len(spreads),'spread_missing_tasks':len(rows)-len(spreads),
            'missing_first_body_readers':missing_readers,'invalid_reader_intervals':invalid,
            'definition':'Per Task maximum paired reader first-body-to-terminal on client local monotonic clock; includes controlled generation and product delivery, not pure network latency',
            'reason':'Old summaries without these timestamps or explicit aggregates remain missing; elapsed_seconds is not substituted'}


async def connect(dsn,**kwargs):
    import asyncpg
    return await asyncpg.connect(dsn,**kwargs)


async def sample(dsn,taskids):
    """One connection, sequential reads in a read-only transaction. No resets."""
    validate_dsn(dsn)
    ids=sorted(set(taskids))
    if any(not isinstance(value,str) or len(value)>36 for value in ids):raise ValueError('Invalid Task IDs')
    connection=await connect(dsn,timeout=10,command_timeout=15,server_settings={'application_name':'capacity-metrics'})
    try:
        await connection.execute('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        tasks=[dict(row) for row in await connection.fetch(TASK_SQL,ids)]
        activity=[dict(row) for row in await connection.fetch(ACTIVITY_SQL)]
        available=await connection.fetchval("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='pg_stat_statements')")
        statements=dict(await connection.fetchrow(STATEMENTS_SQL)) if available else None
        timestamp=await connection.fetchval('SELECT clock_timestamp()')
        await connection.execute('ROLLBACK')
    finally:await connection.close()
    return {'sampled_at_pg':timestamp,'task_count':len(tasks),'requested_count':len(ids),
        'missing_task_ids':sorted(set(ids)-{row['id'] for row in tasks}), 'tasks':tasks,
        'queue_p95_seconds':p95(row['queue_seconds'] for row in tasks),
        'execution_p95_seconds':p95(row['execution_seconds'] for row in tasks),
        'queue_sample_count':sum(row['queue_seconds'] is not None and row['queue_seconds']>=0 for row in tasks),
        'execution_sample_count':sum(row['execution_seconds'] is not None and row['execution_seconds']>=0 for row in tasks),
        'negative_interval_task_ids':[row['id'] for row in tasks if any(row[key] is not None and row[key]<0 for key in ('queue_seconds','execution_seconds'))],
        'activity':activity,'pg_stat_statements':statements,'pg_stat_statements_available':available,
        'delivery':delivery(),
        'definitions':{'queue':'PG tasks.created_at to first durable REPLY_START.created_at; includes admission/dispatch/persistence',
            'execution':'First durable REPLY_START to last durable REPLY_END for terminal Tasks; includes HITL waits and resume intervals, not CPU time',
            'pg_stat_statements':'Cumulative database-wide totals; compare before/after, never reset; includes monitoring queries',
            'activity':'Point-in-time state/wait sample, not historical wait duration',
            'clock':'PG transaction timestamp defaults only; no cross-host monotonic subtraction'}}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dsn',required=True);parser.add_argument('--summary',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    summary=json.loads(args.summary.read_text()) if args.summary else {}
    result=asyncio.run(sample(args.dsn,task_ids(summary)));result['delivery']=delivery(summary)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,default=str)+'\n')
    print(json.dumps({'output':str(args.output),'task_count':result['task_count']}))


if __name__=='__main__':main()
