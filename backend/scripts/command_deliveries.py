#!/usr/bin/env python3
"""Inspect command dispatch or explicitly redrive quarantined unclaimed intent.

Installed command: workmate-command-deliveries. Development: python -m scripts.command_deliveries.
Connection arguments name environment variables, never expose secret DSNs.
"""
import argparse
import asyncio
import json
import os
import sys


class OperationError(ValueError):
    pass


def parser():
    result=argparse.ArgumentParser(description=__doc__)
    result.add_argument('--database-env',default='DATABASE_URL')
    commands=result.add_subparsers(dest='action',required=True)
    inspect=commands.add_parser('inspect',help='Read-only bounded dispatch metadata, no prompts or credentials')
    inspect.add_argument('--command-id')
    inspect.add_argument('--limit',type=int,default=100)
    redrive=commands.add_parser('redrive',help='Explicit authorized retry; never re-executes claimed commands')
    redrive.add_argument('--command-id',required=True)
    redrive.add_argument('--operator-id',required=True,help='Current active root administrator user ID')
    redrive.add_argument('--generation',required=True,type=int,help='Exact latest quarantined generation')
    redrive.add_argument('--node',required=True,help='Expected existing storage node; cannot relocate files')
    redrive.add_argument('--reason',required=True,help='Operational reason saved in security audit; no secrets')
    redrive.add_argument('--check',action='store_true',help='Validate then roll back, without persistent changes')
    return result


def db_url(name):
    value=os.environ.get(name,'')
    if value.startswith('postgresql://'):
        value=value.replace('postgresql://','postgresql+asyncpg://',1)
    if not value.startswith('postgresql+asyncpg://'):
        raise OperationError('postgresql_database_environment_required')
    return value


async def run(args):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
    from workmate_product.persistence.repositories.command_delivery_operations import inspect_deliveries,inspect_quarantined_messages
    from workmate_product.application.command_delivery_operations import redrive_command
    from workmate_product.application.task_capacity import TaskCapacityPolicy
    from app.config import Settings
    engine=create_async_engine(db_url(args.database_env))
    try:
        async with async_sessionmaker(engine,expire_on_commit=False)() as db:
            async with db.begin():
                if args.action=='inspect':
                    await db.execute(text('SET TRANSACTION READ ONLY'))
                    return {'mode':'inspect','rows':await inspect_deliveries(db,command_id=args.command_id,limit=args.limit),
                            'quarantined_messages':await inspect_quarantined_messages(db,command_id=args.command_id,limit=args.limit)}
                settings=Settings(_env_file=None)
                from app.services.knowledge import knowledge_settings
                generation=await redrive_command(db,args.command_id,operator_id=args.operator_id,
                    expected_generation=args.generation,storage_node_id=args.node,reason=args.reason,
                    knowledge_enabled=knowledge_settings().enabled,policy=TaskCapacityPolicy.from_settings(settings))
                if args.check:
                    await db.rollback()
                return {'mode':'check' if args.check else 'redrive','command_id':args.command_id,
                        'delivery_generation':generation,'committed':not args.check}
    finally:
        await engine.dispose()


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        print(json.dumps(asyncio.run(run(args)),ensure_ascii=False,default=str,indent=2))
        return 0
    except Exception as error:
        from workmate_product.application.commands import CommandRejected
        # Exception strings from drivers/providers may contain credentials.
        code=str(error) if isinstance(error,(OperationError,CommandRejected)) else 'command_delivery_operation_failed'
        print(json.dumps({'error':code,'error_type':type(error).__name__}),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
