"""Explicit bounded maintenance, independent of task execution and projection."""
from ..persistence.repositories import tool_content_retention as repository


def bounds(retention_days,limit):
    if type(retention_days) is not int or not 1<=retention_days<=36500:
        raise ValueError('Retention days must be between 1 and 36500')
    if type(limit) is not int or not 1<=limit<=1000:
        raise ValueError('Batch size must be between 1 and 1000')


async def compact_transport(sessions,*,retention_days=30,limit=100,apply=False):
    bounds(retention_days,limit)
    result=dict(mode='apply' if apply else 'check',runtime_events=0,product_events=0,outbox=0)
    async with sessions.begin() as db:
        candidates=await repository.candidate_conversations(db,days=retention_days,limit=limit)
    for cid in candidates:
        async with sessions.begin() as db:
            batch=await repository.batch(db,cid,days=retention_days,
                limit=limit-result['runtime_events'],apply=apply)
        for name,count in batch.items(): result[name]+=count
        if result['runtime_events']>=limit: break
    return result
