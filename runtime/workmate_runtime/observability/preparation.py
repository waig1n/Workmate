"""Monotonic preparation timings; never record inputs or exception messages."""
from contextlib import contextmanager
import logging
import sys
import time
from threading import Lock

logger = logging.getLogger(__name__)

_handler_lock = Lock()


def _ensure_output():
    # Worker may configure only third-party logging. Own exactly this module's
    # INFO sink, with propagation disabled so root handlers cannot duplicate it.
    with _handler_lock:
        if not any(getattr(handler, '_preparation_sink', False) for handler in logger.handlers):
            handler = logging.StreamHandler()
            handler._preparation_sink = True
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
            logger.addHandler(handler)
        for handler in logger.handlers:
            if getattr(handler, '_preparation_sink', False) and handler.stream is not sys.stderr:
                # setStream flushes the old target, which may already be closed
                # after capture/redirection. StreamHandler emits synchronously.
                handler.acquire()
                try:
                    handler.stream = sys.stderr
                finally:
                    handler.release()
        logger.setLevel(logging.INFO)
        logger.propagate = False


@contextmanager
def bootstrap_stage(workspace_id, stage):
    """Time pre-binding workspace work using only its safe internal identity."""
    _ensure_output()
    started = time.monotonic()
    error_type = None
    try:
        yield
    except BaseException as error:
        error_type = type(error).__name__
        raise
    finally:
        elapsed = (time.monotonic() - started) * 1000
        outcome = 'failed' if error_type else 'completed'
        fields = dict(workspace_id=workspace_id, stage=stage, duration_ms=elapsed,
                      outcome=outcome, error_type=error_type)
        logger.info('Runtime bootstrap workspace_id=%s stage=%s duration_ms=%.3f outcome=%s error_type=%s',
                    workspace_id, stage, elapsed, outcome, error_type or '-', extra=fields)


@contextmanager
def preparation_stage(owner, stage, *, resume):
    """Measure one internal stage without retaining any assembled resources."""
    _ensure_output()
    started = time.monotonic()
    error_type = None
    try:
        yield
    except BaseException as error:
        error_type = type(error).__name__
        raise
    finally:
        elapsed = (time.monotonic() - started) * 1000
        outcome = 'failed' if error_type else 'completed'
        fields = dict(task_id=owner.task_id, session_id=owner.session_id,
                      worker_id=owner.worker_id, stage=stage, resume=resume,
                      duration_ms=elapsed, outcome=outcome, error_type=error_type)
        logger.info(
            'Runtime preparation task_id=%s session_id=%s worker_id=%s stage=%s '
            'resume=%s duration_ms=%.3f outcome=%s error_type=%s',
            owner.task_id, owner.session_id, owner.worker_id, stage,
            resume, elapsed, outcome, error_type or '-', extra=fields,
        )
