"""Offline-only tool object cleanup; preview unless explicitly applied."""
import asyncio
from workmate_product.application.tool_content_gc import report,delete_candidates,bounds

async def main():
    import argparse
    import json
    from app.config import get_settings
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from workmate_product.artifact_storage import tool_content_store_from_settings
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--writers-stopped',action='store_true',
        help='Confirm all publishers stay stopped and this bucket belongs exclusively to this database')
    parser.add_argument('--grace-days',type=int,default=7)
    parser.add_argument('--limit',type=int,default=100)
    args=parser.parse_args()
    bounds(args.grace_days,args.limit)
    if args.apply and not args.writers_stopped: parser.error('--apply requires --writers-stopped')
    settings=get_settings()
    engine=create_async_engine(settings.database_url)
    redis=None
    try:
        sessions=async_sessionmaker(engine)
        objects=await report(settings,sessions,grace_days=args.grace_days)
        result={'mode':'check','objects':objects}
        if args.apply:
            from redis.asyncio import Redis
            if not settings.coordination_redis_url: raise ValueError('Redis registry must be reachable')
            redis=Redis.from_url(str(settings.coordination_redis_url),socket_connect_timeout=5,socket_timeout=5)
            candidates=[item for item in objects if item['status']=='orphan_candidate'][:args.limit]
            result={'mode':'apply','deleted':await delete_candidates(sessions,
                tool_content_store_from_settings(settings),redis,namespace=settings.coordination_namespace,
                candidates=candidates,writers_stopped=True,grace_days=args.grace_days,limit=args.limit)}
        print(json.dumps(result))
    finally:
        if redis is not None: await redis.aclose()
        await engine.dispose()


if __name__ == '__main__': asyncio.run(main())
