#!/usr/bin/env python3
"""Preview or prune one bounded batch of reconstructable old tool transport."""
import argparse
import asyncio
import json
from workmate_product.application.tool_content_retention import compact_transport

async def main():
    from app.config import get_settings
    from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--retention-days',type=int,default=30)
    parser.add_argument('--limit',type=int,default=100)
    args=parser.parse_args()
    engine=create_async_engine(get_settings().database_url)
    try:
        print(json.dumps(await compact_transport(async_sessionmaker(engine),
            retention_days=args.retention_days,limit=args.limit,apply=args.apply)))
    finally: await engine.dispose()

if __name__=='__main__': asyncio.run(main())
