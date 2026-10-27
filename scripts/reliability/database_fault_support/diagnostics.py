"""Read-only SQLAlchemy connection lifecycle trace for the owned PG fault."""
import asyncio
import json
from pathlib import Path
import time
import traceback
from collections import Counter,deque
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import Pool


def install():
    destination=Path('/evidence/pg-connection-trace.jsonl')
    recent=deque(maxlen=256);last_checkout={};counts=Counter()
    def trace(kind,dbapi=None,record=None,**details):
        counts[kind]+=1
        driver=getattr(dbapi,'driver_connection',None)
        # SQLAlchemy events can run inside greenlet switches. Never inspect
        # Task coroutine frames or cr_await/gi_yieldfrom/ag_await here: even
        # traversing their truth values crashed the historical fault observer.
        # Task name and cancellation count are sufficient correlation fields.
        try:task=asyncio.current_task()
        except RuntimeError:task=None
        value={'time':time.monotonic(),'event':kind,'dbapi_id':hex(id(dbapi)) if dbapi else None,
            'driver_id':hex(id(driver)) if driver else None,'record_id':hex(id(record)) if record else None,
            'task':task.get_name() if task else None,'cancel_count':task.cancelling() if task else None,
            **details}
        if kind=='checkout':last_checkout[value['record_id']]=value
        recent.append(value)
        if kind in {'invalidate','sql_error','close'} or details.get('asyncio_safe') is False:
            value['stack']=[{'file':f.filename,'line':f.lineno,'name':f.name}
                for f in traceback.extract_stack(limit=25)[:-1]]
            value['last_checkout']=last_checkout.get(value['record_id'])
            with destination.open('a') as file:file.write(json.dumps(value)+'\n')
    @event.listens_for(Pool,'checkout')
    def checkout(dbapi,record,proxy):trace('checkout',dbapi,record)
    @event.listens_for(Pool,'checkin')
    def checkin(dbapi,record):trace('checkin',dbapi,record)
    @event.listens_for(Pool,'connect')
    def connect(dbapi,record):trace('connect',dbapi,record)
    @event.listens_for(Pool,'close')
    def close(dbapi,record):trace('close',dbapi,record)
    @event.listens_for(Pool,'invalidate')
    def invalidate(dbapi,record,error):trace('invalidate',dbapi,record,error_type=type(error).__name__)
    @event.listens_for(Pool,'reset')
    def reset(dbapi,record,state):trace('reset',dbapi,record,terminate_only=state.terminate_only,
        asyncio_safe=state.asyncio_safe,transaction_was_reset=state.transaction_was_reset)
    @event.listens_for(Engine,'handle_error')
    def error(context):
        # Do not access SQL parameters, URLs, credentials or arbitrary locals.
        trace('sql_error',error_type=type(context.original_exception).__name__,
            is_disconnect=context.is_disconnect)
    def flush():
        Path('/evidence/pg-connection-summary.json').write_text(json.dumps({'counts':dict(counts),
            'recent':list(recent),'last_checkout':last_checkout},indent=2))
    return flush
