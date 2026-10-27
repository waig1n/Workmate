"""A dead ready Worker must terminate its HTTP server, without leaking secrets."""
import asyncio
import inspect
import logging
from pathlib import Path
import sys
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
import pytest

from workmate_runtime.config import RuntimeSettings
from workmate_runtime.roles import worker


SECRET = 'postgresql://worker:fixture-private-password@db/private?token=fixture-private-token'


class WorkerLoopFailure(RuntimeError):
    pass


def _install_server(monkeypatch, outcome):
    """Exercise the real role lifespan through a bounded, socket-free Server."""
    observed = SimpleNamespace(entered=False, closed=False, fatal_exit=False,
                               admission=None, health=None, error_line=None, fatal_before_cleanup=False)
    settings = SimpleNamespace(runtime=RuntimeSettings(reme_enabled=False), storage_node_id='node-a')

    async def fail_inside_worker(settings, shutdown, *, on_ready):
        private_database_url = SECRET
        if outcome == 'startup-cancelled':
            raise asyncio.CancelledError(private_database_url)
        ready = asyncio.Event()
        ready.set()
        on_ready(SimpleNamespace(service=SimpleNamespace(ready=ready, active_sessions=set(), failures={}),
                                 knowledge_management=object()))
        if outcome == 'shutdown':
            await shutdown.wait()
            return
        await observed.trigger.wait()
        if outcome == 'cleanup':
            ready.clear()
            observed.cleanup_started.set()
            await observed.cleanup_release.wait()
            raise WorkerLoopFailure(private_database_url)
        if outcome == 'error':
            observed.error_line = inspect.currentframe().f_lineno + 1
            raise WorkerLoopFailure(private_database_url)
        if outcome == 'cancelled':
            raise asyncio.CancelledError(private_database_url)
        assert outcome == 'returned'

    class Server:
        def __init__(self, config):
            self.config = config
            self.started = False
            self._should_exit = False
            self.exit_requested = asyncio.Event()

        @property
        def should_exit(self):
            return self._should_exit

        @should_exit.setter
        def should_exit(self, value):
            self._should_exit = value
            if value:
                self.exit_requested.set()

        async def serve(self, *args, **kwargs):
            web = self.config.app
            observed.trigger = asyncio.Event()
            observed.cleanup_started = asyncio.Event()
            observed.cleanup_release = asyncio.Event()
            async with web.router.lifespan_context(web):
                self.started = True
                observed.entered = True
                assert web.state.accepting_knowledge is True
                async with AsyncClient(transport=ASGITransport(app=web), base_url='http://worker') as client:
                    assert (await client.get('/health')).status_code == 200
                    if outcome == 'shutdown':
                        # Uvicorn's ordinary shutdown leaves the role to drain
                        # through its lifespan, without an unexpected task exit.
                        self.should_exit = True
                    else:
                        observed.trigger.set()
                        try:
                            await asyncio.wait_for(self.exit_requested.wait(), 2)
                            observed.fatal_exit = self.should_exit
                            observed.admission = web.state.accepting_knowledge
                            observed.health = (await client.get('/health')).status_code
                            observed.fatal_before_cleanup = (
                                observed.cleanup_started.is_set()
                                and not observed.cleanup_release.is_set())
                        finally:
                            observed.cleanup_release.set()
            observed.closed = True

    import uvicorn
    monkeypatch.setattr(uvicorn, 'Server', Server)
    # The old blocking convenience entrypoint cannot supervise the ready task.
    def unsupervised(*args, **kwargs):
        pytest.fail('Worker must connect background failure to its uvicorn.Server')
    monkeypatch.setattr(uvicorn, 'run', unsupervised)
    monkeypatch.setattr(worker, 'serve_worker', fail_inside_worker)
    monkeypatch.setattr(worker, 'load_worker_role_settings', lambda: settings)
    monkeypatch.setattr(sys, 'argv', ['workmate-worker'])
    return observed


@pytest.mark.parametrize('outcome', ['error', 'returned', 'cancelled'])
def test_ready_worker_loss_closes_admission_and_exits_server_nonzero(monkeypatch, outcome):
    observed = _install_server(monkeypatch, outcome)
    with pytest.raises(SystemExit) as result:
        worker.main()
    assert result.value.code == 1
    assert observed.entered
    assert observed.fatal_exit
    assert observed.admission is False
    assert observed.health == 503


def test_normal_server_shutdown_drains_without_fatal_exit(monkeypatch, caplog):
    observed = _install_server(monkeypatch, 'shutdown')
    caplog.set_level(logging.ERROR, logger=worker.__name__)
    worker.main()
    assert observed.entered and observed.closed
    assert not observed.fatal_exit
    assert not caplog.records


def test_readiness_loss_requests_exit_before_blocked_worker_cleanup_finishes(monkeypatch):
    observed = _install_server(monkeypatch, 'cleanup')
    with pytest.raises(SystemExit) as result:
        worker.main()
    assert result.value.code == 1
    assert observed.fatal_exit and observed.fatal_before_cleanup
    assert observed.admission is False
    assert observed.health == 503


def test_worker_failure_logs_source_frames_but_never_exception_text_or_locals(monkeypatch, caplog):
    observed = _install_server(monkeypatch, 'error')
    caplog.set_level(logging.ERROR, logger=worker.__name__)
    with pytest.raises(SystemExit):
        worker.main()
    assert observed.entered
    assert 'WorkerLoopFailure' in caplog.text
    assert Path(__file__).name in caplog.text
    assert 'fail_inside_worker' in caplog.text
    assert str(observed.error_line) in caplog.text
    assert SECRET not in caplog.text
    assert 'fixture-private-password' not in caplog.text
    assert 'fixture-private-token' not in caplog.text
    assert 'private_database_url' not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize('outcome', ['error', 'shutdown'])
def test_watchdog_is_armed_only_on_fatal_and_cancelled_after_asyncio_run(monkeypatch, outcome):
    import threading
    observed = _install_server(monkeypatch, outcome)
    timers = []

    class Timer:
        def __init__(self, interval, function, args=None, kwargs=None):
            self.interval = interval
            self.started = False
            self.cancelled = False
            self.daemon = False
            timers.append(self)

        def start(self):
            self.started = True
            assert self.daemon
            assert self.interval > 0

        def cancel(self):
            # Cancelling inside the coroutine leaves asyncio.run's final task
            # reclamation unbounded when an orphan ignores cancellation.
            with pytest.raises(RuntimeError, match='no running event loop'):
                asyncio.get_running_loop()
            self.cancelled = True

    monkeypatch.setattr(threading, 'Timer', Timer)
    if outcome == 'error':
        with pytest.raises(SystemExit) as result:
            worker.main()
        assert result.value.code == 1
        armed = [timer for timer in timers if timer.started]
        assert len(armed) == 1
        assert armed[0].cancelled
        assert observed.fatal_exit
    else:
        worker.main()
        assert observed.closed
        assert not any(timer.started for timer in timers)


@pytest.mark.parametrize('phase', ['lifespan', 'asyncio-finalization'])
def test_watchdog_forces_exit_when_cleanup_ignores_cancellation(phase):
    import os
    import subprocess
    import textwrap
    import time

    script = textwrap.dedent("""
        import asyncio
        import sys
        import time
        from types import SimpleNamespace
        from workmate_runtime.config import RuntimeSettings
        from workmate_runtime.roles import worker

        # Refuse old implementations before entering any real network server.
        assert hasattr(worker, 'run_worker_server')
        assert hasattr(worker, 'shutdown_budget_seconds')
        settings = SimpleNamespace(runtime=RuntimeSettings(reme_enabled=False), storage_node_id='node')
        worker.load_worker_role_settings = lambda: settings
        worker.shutdown_budget_seconds = lambda settings: 0.1
        phase = sys.argv.pop()
        sys.argv = ['workmate-worker']

        async def stuck():
            print('CLEANUP_START=' + str(time.time()), flush=True)
            while True:
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    pass

        async def run(settings, *, on_fatal=None):
            if phase == 'asyncio-finalization':
                asyncio.create_task(stuck())
                await asyncio.sleep(0)
                on_fatal()
                return  # asyncio.run must now reclaim the cancellation-resistant task.
            trigger, failed = asyncio.Event(), asyncio.Event()
            async def serve(settings, shutdown, *, on_ready):
                ready = asyncio.Event()
                ready.set()
                on_ready(SimpleNamespace(service=SimpleNamespace(ready=ready), knowledge_management=None))
                await trigger.wait()
                ready.clear()
                await stuck()
            worker.serve_worker = serve
            def fatal():
                on_fatal()
                failed.set()
            web = worker.create_worker_app(settings, on_fatal=fatal)
            async with web.router.lifespan_context(web):
                trigger.set()
                await failed.wait()
            # The lifespan's final cleanup must also be bounded.
        worker.run_worker_server = run
        worker.main()
    """)
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(str(root / path) for path in
            ('runtime', 'packages/contracts/src', 'packages/product/src'))}
    result = subprocess.run([sys.executable, '-c', script, phase], env=env,
                            capture_output=True, text=True, timeout=15)
    markers = [line for line in result.stdout.splitlines() if line.startswith('CLEANUP_START=')]
    assert markers, result.stdout + result.stderr
    assert result.returncode == 1, result.stdout + result.stderr
    assert time.time() - float(markers[0].split('=', 1)[1]) < 1.0
    assert SECRET not in result.stdout + result.stderr


def test_startup_self_cancellation_is_sanitized_and_arms_watchdog(monkeypatch, caplog):
    import threading
    observed = _install_server(monkeypatch, 'startup-cancelled')
    watchdog = []
    monkeypatch.setattr(threading, 'Timer', lambda *args, **kwargs: SimpleNamespace(
        daemon=False, start=lambda: watchdog.append('armed'),
        cancel=lambda: watchdog.append('cancelled')))
    caplog.set_level(logging.ERROR, logger=worker.__name__)
    with pytest.raises(SystemExit) as result:
        worker.main()
    assert result.value.code == 1
    assert not observed.entered
    assert watchdog == ['armed', 'cancelled']
    assert caplog.records
    assert SECRET not in caplog.text
    assert 'fixture-private-password' not in caplog.text
    assert 'fixture-private-token' not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
