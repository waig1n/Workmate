"""Explicit existing-deployment upgrade: python -m scripts.apply_builtin_defaults."""
import argparse
import asyncio
import json

from app.db import dispose_engine, get_sessionmaker
from app.services.builtin_defaults import apply_builtin_defaults


def parser():
    result=argparse.ArgumentParser(description='Preview or add organization-wide builtin Skill grants and default assistant bindings. Does not enable disabled Skills or remove existing bindings.')
    mode=result.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check',action='store_true',help='Read-only preview')
    mode.add_argument('--apply',action='store_true',help='Apply defaults, including restoring revoked general builtin Skill grants')
    return result


async def run(apply):
    try:
        async with get_sessionmaker().begin() as db:
            report=await apply_builtin_defaults(db,apply=apply)
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return 2 if report['blockers'] else 0
    finally:
        await dispose_engine()


def main():
    args=parser().parse_args()
    raise SystemExit(asyncio.run(run(args.apply)))


if __name__=='__main__':
    main()
