"""Stage hints in the caller transaction; publish only after its outer commit.

Savepoint rollback discards only that savepoint's hints. Publication never owns
or extends a database transaction and is intentionally best effort.
"""
from sqlalchemy import event
from sqlalchemy.orm import Session

_KEY = 'workmate_wakeups'


def stage_wakeup(db, key):
    session = db.sync_session
    if session.info.get('wakeup_bus') is None:
        return
    transaction = session.get_nested_transaction() or session.get_transaction()
    if transaction is None:
        raise RuntimeError('Wakeup requires a caller-owned transaction')
    session.info.setdefault(_KEY, {}).setdefault(transaction, set()).add(key)


@event.listens_for(Session, 'after_commit')
def _committed(session):
    transaction = session.get_nested_transaction() or session.get_transaction()
    pending = session.info.get(_KEY, {})
    keys = pending.pop(transaction, set())
    if transaction is not None and transaction.parent is not None:
        pending.setdefault(transaction.parent, set()).update(keys)
    else:
        bus = session.info.get('wakeup_bus')
        if bus is not None:
            for key in keys:
                bus.publish_nowait(key)


@event.listens_for(Session, 'after_rollback')
def _rolled_back(session):
    transaction = session.get_nested_transaction() or session.get_transaction()
    session.info.get(_KEY, {}).pop(transaction, None)


@event.listens_for(Session, 'after_transaction_end')
def _ended(session, transaction):
    pending = session.info.get(_KEY, {})
    pending.pop(transaction, None)
    if transaction.parent is None:
        session.info.pop(_KEY, None)
