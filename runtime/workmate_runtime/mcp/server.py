"""Authenticated sandbox gateway with actor-owned MCP Session transports."""
import argparse
import asyncio
import secrets
from contextlib import asynccontextmanager

from agentscope.mcp import MCPClient
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .actor import SessionMCPActor, MCPActorError
from .http_transport import governed_client_factory


class GatewayState:
    def __init__(self, client_factory=governed_client_factory):
        self.actors = {}
        self.lock = asyncio.Lock()
        self.client_factory = client_factory

    async def close(self):
        errors = []
        for key, actor in tuple(self.actors.items()):
            try:
                await actor.submit('close')
            except BaseException as error:
                errors.append(error)
            else:
                self.actors.pop(key)
        if errors:
            raise BaseExceptionGroup('MCP Session shutdown is incomplete', errors)


def build_app(state, *, auth_token, instance_nonce):
    if not auth_token or not instance_nonce:
        raise ValueError('Gateway authentication and nonce are mandatory')

    @asynccontextmanager
    async def lifespan(app):
        yield
        await state.close()

    app = FastAPI(title='workmate-session-mcp-gateway', lifespan=lifespan)

    @app.middleware('http')
    async def authorize(request, call_next):
        if request.url.path != '/health':
            header = request.headers.get('authorization', '')
            if not header.isascii() or not secrets.compare_digest(header, f'Bearer {auth_token}'):
                return PlainTextResponse('Invalid gateway credential', status_code=401)
        return await call_next(request)

    @app.get('/health')
    async def health():
        return {'status':'ok', 'instance_nonce':instance_nonce}

    def lookup(agent_id, session_id, name):
        actor = state.actors.get((agent_id, session_id, name))
        if actor is None:
            raise HTTPException(404, 'MCP Session configuration not found')
        return actor

    @app.post('/mcps')
    async def add(request: Request, agent_id: str = '', session_id: str = ''):
        if not agent_id or not session_id:
            raise HTTPException(400, 'Trusted Agent and Session identities are required')
        try:
            body = await request.json()
            client = state.client_factory(body)
        except Exception:
            raise HTTPException(400, 'Invalid MCP configuration') from None
        key = (agent_id, session_id, client.name)
        async with state.lock:
            if key in state.actors:
                raise HTTPException(409, 'MCP Session configuration already exists')
            actor = SessionMCPActor(client)
            # Register before connection. Failure retains the partially opened
            # infrastructure; do not silently start a second STDIO server.
            state.actors[key] = actor
        try:
            await actor.ready()
        except MCPActorError:
            raise HTTPException(503, 'MCP Session initialization failed') from None
        return {'ok': True}

    @app.delete('/mcps/{name}')
    async def remove(name: str, agent_id: str = '', session_id: str = ''):
        async with state.lock:
            actor = lookup(agent_id, session_id, name)
            try:
                await actor.submit('close')
            except MCPActorError:
                raise HTTPException(409, 'MCP Session still has unresolved resources') from None
            del state.actors[(agent_id, session_id, name)]
        return {'ok': True}

    @app.get('/mcps/{name}/tools')
    async def tools(name: str, agent_id: str = '', session_id: str = ''):
        try:
            raw = await lookup(agent_id, session_id, name).submit('tools')
        except MCPActorError:
            raise HTTPException(503, 'MCP Session operation did not complete') from None
        return [tool.model_dump(mode='json') for tool in raw]

    @app.post('/mcps/{name}/tools/{tool:path}')
    async def call(name: str, tool: str, request: Request,
                   agent_id: str = '', session_id: str = ''):
        try:
            body = await request.json()
            arguments = body.get('arguments', {})
            if not isinstance(arguments, dict):
                raise ValueError()
        except Exception:
            raise HTTPException(400, 'MCP arguments must be an object') from None
        try:
            chunk = await lookup(agent_id, session_id, name).submit('call', tool, arguments)
        except MCPActorError:
            raise HTTPException(503, 'MCP operation has an unknown outcome') from None
        return {'chunk': chunk.model_dump(mode='json')}

    return app


async def run(port, auth_token, instance_nonce):
    import uvicorn
    app = build_app(GatewayState(), auth_token=auth_token, instance_nonce=instance_nonce)
    await uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
        access_log=False, log_level='warning')).serve()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--auth-token', required=True)
    parser.add_argument('--instance-nonce', required=True)
    args = parser.parse_args()
    asyncio.run(run(args.port, args.auth_token, args.instance_nonce))


if __name__ == "__main__":
    main()
