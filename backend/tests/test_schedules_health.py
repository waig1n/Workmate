"""Gateway readiness requires a live enabled Schedule loop."""
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
import pytest

from app.main import build_app


@pytest.mark.parametrize('enabled,state,status', [
    (True, 'missing', 503), (True, 'done', 503), (True, 'running', 200),
    (False, 'missing', 200), (False, 'done', 200),
])
async def test_schedule_loop_health(monkeypatch, enabled, state, status):
    monkeypatch.setattr('app.main.get_settings', lambda: SimpleNamespace(schedule_enabled=enabled,channel_enabled=False))
    app = build_app()
    app.state.product_ready = True
    for name in ('projection_task', 'command_publisher_task', 'business_publisher_task'):
        setattr(app.state, name, SimpleNamespace(done=lambda: False))
    if state != 'missing':
        app.state.schedule_task = SimpleNamespace(done=lambda: state == 'done')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://gateway') as http:
        response = await http.get('/health')
    assert response.status_code == status
