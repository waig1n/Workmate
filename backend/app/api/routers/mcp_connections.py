"""Organization MCP connection management; private fields never enter public DTOs."""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import get_current_user
from ...config import get_settings
from ...db import get_db
from ...services import mcp_probe
from workmate_product.persistence.models import User
from workmate_product.mcp import repository
from workmate_product.mcp.custom import CUSTOM_HUB, editable_configuration, parse_custom
from workmate_product.mcp.definitions import MCPConfiguration, MCPDefinitionError
from .mcps import _ERRORS, _failure, _view

router = APIRouter(prefix='/mcps', tags=['mcps'])


async def _body(request):
    # FastAPI's default validation errors include the rejected input. These
    # routes carry secrets, so validation always produces a fixed safe error.
    try:
        raw = await request.body()
        if len(raw) > 32768:
            raise ValueError('Body too large')
        import json
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError('Object required')
        return payload
    except Exception:
        raise HTTPException(422, 'Invalid MCP configuration request') from None


@router.post('/custom', status_code=201)
async def create_custom(request: Request, user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    payload = await _body(request)
    scope_id = payload.pop('scope_id', None)
    actor_id = user.id
    try:
        if not db.in_transaction():
            await db.begin()
        if not isinstance(scope_id, str):
            raise MCPDefinitionError('Organization scope is required', 422)
        await repository.authorize_mcp_install(db, actor_user_id=actor_id, scope_id=scope_id)
        config = parse_custom(payload)
        installed = await repository.install_mcp(db, actor_user_id=actor_id,
            scope_id=scope_id, card=config.card(), values={})
        response = await _view(db, actor_id, installed)
        await db.commit()
        return response
    except _ERRORS as exc:
        await db.rollback()
        raise _failure(exc) from exc


@router.get('/{installation_id}/configuration')
async def configuration(installation_id: str, user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    try:
        if not db.in_transaction():
            await db.begin()
        installed, private = await repository.managed_mcp_definition(db,
            actor_user_id=user.id, installation_id=installation_id)
        return editable_configuration(installed, private)
    except _ERRORS as exc:
        raise _failure(exc) from exc


@router.patch('/{installation_id}/custom')
async def update_custom(installation_id: str, request: Request, user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    payload = await _body(request)
    actor_id = user.id
    try:
        if not db.in_transaction():
            await db.begin()
        installed, private = await repository.managed_mcp_definition(db,
            actor_user_id=actor_id, installation_id=installation_id)
        if private.card.hub_id != CUSTOM_HUB:
            raise MCPDefinitionError('This connection uses a catalog template', 409)
        previous = {'name': installed.name, 'display_name': installed.display_name,
            'description': installed.metadata_.get('description', ''),
            'url': private.client.mcp_config.url, 'headers': private.client.mcp_config.headers or {},
            'timeout': private.client.mcp_config.timeout, 'is_stateful': private.client.is_stateful}
        config = parse_custom({**previous, **payload})
        installed = await repository.update_mcp(db, actor_user_id=actor_id,
            installation_id=installation_id, card=config.card(card_id=private.card.id),
            values={}, name=config.name)
        response = await _view(db, actor_id, installed)
        await db.commit()
        return response
    except _ERRORS as exc:
        await db.rollback()
        raise _failure(exc) from exc


async def _probe(request, config):
    runtime = getattr(request.app.state, 'product_runtime', None)
    directory = getattr(getattr(runtime, 'admission', None), 'node_directory', None)
    return await mcp_probe.probe_mcp(config, settings=get_settings(), node_directory=directory)


@router.post('/probe')
async def probe_draft(request: Request, user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    payload = await _body(request)
    actor_id = user.id
    try:
        if not db.in_transaction():
            await db.begin()
        scope_id = payload.get('scope_id')
        if not isinstance(scope_id, str) or set(payload) != {'scope_id', 'configuration'}:
            raise MCPDefinitionError('Organization scope and HTTP configuration are required', 422)
        await repository.authorize_mcp_install(db, actor_user_id=actor_id, scope_id=scope_id)
        try:
            declaration = payload['configuration']
            if not isinstance(declaration, dict):
                raise ValueError('Configuration object required')
            config = MCPConfiguration.model_validate({'name': 'mcp-probe', **declaration})
            if config.mcp_config.type != 'http_mcp':
                raise ValueError('HTTP required')
            parse_custom({'name': config.name, 'is_stateful': config.is_stateful,
                'url': config.mcp_config.url, 'headers': config.mcp_config.headers or {},
                'timeout': config.mcp_config.timeout})
        except MCPDefinitionError:
            raise
        except Exception:
            raise MCPDefinitionError('Invalid HTTP MCP configuration', 422, code='invalid_configuration') from None
        await db.rollback()  # Network I/O must not hold the organization lock.
        result = await _probe(request, config)
        await db.begin()
        await repository.authorize_mcp_install(db, actor_user_id=actor_id, scope_id=scope_id)
        return result
    except _ERRORS as exc:
        await db.rollback()
        raise _failure(exc) from exc


@router.post('/{installation_id}/probe')
async def probe_saved(installation_id: str, request: Request, user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    actor_id = user.id
    try:
        if not db.in_transaction():
            await db.begin()
        installed, private = await repository.managed_mcp_definition(db,
            actor_user_id=actor_id, installation_id=installation_id)
        if not installed.enabled:
            raise MCPDefinitionError('Enable the connection before testing it', 409)
        await db.rollback()
        result = await _probe(request, private.client)
        await db.begin()
        current_installation, current = await repository.managed_mcp_definition(db,
            actor_user_id=actor_id, installation_id=installation_id)
        if not current_installation.enabled:
            raise MCPDefinitionError('Connection was disabled during the test', 409)
        if current.secret_version != private.secret_version:
            raise MCPDefinitionError('Connection changed during the test; test the saved version again', 409)
        return result
    except _ERRORS as exc:
        await db.rollback()
        raise _failure(exc) from exc
