"""Product readiness gates Worker startup after migration and bootstrap."""
from types import SimpleNamespace
import pytest
from httpx import ASGITransport, AsyncClient
from app.main import build_app

@pytest.mark.parametrize('ready,done,status', [(False,False,503),(True,False,200),(True,True,503)])
async def test_health_requires_started_live_projection(ready,done,status):
    app=build_app()
    app.state.product_ready=ready
    app.state.projection_task=SimpleNamespace(done=lambda:done)
    app.state.command_publisher_task=SimpleNamespace(done=lambda:False)
    app.state.business_publisher_task=SimpleNamespace(done=lambda:False)
    app.state.schedule_task=SimpleNamespace(done=lambda:False)
    app.state.channel_task=SimpleNamespace(done=lambda:False)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://product') as client:
        response=await client.get('/health')
    assert response.status_code==status
    assert response.json()=={'role':'product-api','ready':status==200}


@pytest.mark.parametrize('missing',[True,False])
async def test_health_requires_live_command_publisher(missing):
    app=build_app()
    app.state.product_ready=True
    app.state.projection_task=SimpleNamespace(done=lambda:False)
    if not missing:
        app.state.command_publisher_task=SimpleNamespace(done=lambda:True)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://product') as client:
        assert (await client.get('/health')).status_code==503
