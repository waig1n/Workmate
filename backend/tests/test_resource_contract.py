"""Public request/response schemas expose one supported representation."""
from fastapi import FastAPI
from app.api.routers import agents, skills, mcps, tasks, knowledge
from app.api.pagination import page_result as result


def test_resource_contract_always_returns_page_envelopes():
    app = FastAPI()
    for router in (agents.router, skills.router, mcps.router, knowledge.router):
        app.include_router(router)
    schema = app.openapi()
    for name in ('agents', 'skills', 'mcps', 'knowledge-bases'):
        operation = schema['paths'][f'/{name}']['get']
        page = next(p for p in operation['parameters'] if p['name'] == 'page')
        assert page['schema']['default'] == 1
        assert page['schema']['type'] == 'integer'
    assert result([], 0, 1, 12) == {'items': [], 'total': 0, 'page': 1, 'page_size': 12}


def test_confirm_openapi_only_accepts_versioned_batch():
    app = FastAPI()
    app.include_router(tasks.router)
    schema = app.openapi()
    body = schema['paths']['/tasks/{task_id}/confirm']['post']['requestBody']['content']['application/json']['schema']
    assert body == {'$ref': '#/components/schemas/ConfirmBatchRequest'}
    assert set(schema['components']['schemas']['ConfirmBatchRequest']['required']) == {'decisions', 'decision_version'}
