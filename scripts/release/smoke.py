"""Packaging smoke test executed inside an immutable Python image."""
import importlib
import importlib.metadata
import asyncio
import json
from pathlib import Path
import sys
import subprocess


def check_operational_cli(role):
    """Validate the shipped module entrypoint without starting database work."""
    module = {'gateway': 'scripts.apply_builtin_defaults',
              'runtime': 'workmate_runtime.operations.upgrade_check'}[role]
    # Help parsing exits before either command opens its application database.
    # Capture output so the packaging smoke still emits one final JSON report.
    subprocess.run([sys.executable, '-m', module, '--help'], check=True,
                   capture_output=True, text=True, timeout=30)


def check_orm_defaults():
    """Exercise reflected defaults locally; never connect to the application DB."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from workmate_product.persistence.models import Scope, User

    engine = create_engine('sqlite://')
    try:
        # Only these tables are needed; avoid metadata-wide PostgreSQL triggers.
        User.__table__.create(engine)
        Scope.__table__.create(engine)
        cases = [('  Team Alpha  ', 'team alpha'),
                 ('  STRA\u1e9eE  ', 'strasse'), ('   ', '')]
        with Session(engine) as db:
            for index, (name, expected) in enumerate(cases):
                user = User(id=f'u{index}', email=f'  USER{index}@EXAMPLE.TEST  ',
                            password_hash='packaging-fixture', display_name='Fixture')
                db.add(user)
                db.flush()
                scope = Scope(id=f's{index}', kind='personal',
                              owner_user_id=user.id, name=name)
                db.add(scope)
                db.flush()
                assert user.normalized_email == f'user{index}@example.test'
                assert user.created_at is not None
                assert scope.normalized_name == expected
            db.commit()
            db.expire_all()
            assert list(db.scalars(select(Scope.normalized_name).order_by(Scope.id))) == [
                expected for _, expected in cases
            ]
            users = list(db.scalars(select(User).order_by(User.id)))
            assert [user.normalized_email for user in users] == [
                f'user{index}@example.test' for index in range(len(cases))
            ]
            assert all(user.created_at is not None for user in users)
    finally:
        engine.dispose()


def main(role):
    report = json.loads(Path('/usr/local/source-files.json').read_text())
    modules = []
    for entry in report['files']:
        if entry['mode'] != 'source':
            continue
        name = entry['path'][:-3].replace('/', '.')
        if name.endswith('.__init__'):
            name = name[:-9]
        if name.endswith('.__main__'):
            continue
        module = importlib.import_module(name)
        if (not str(module.__file__).startswith('/usr/local/lib/')
                or Path(module.__file__).suffix != '.py'):
            raise ValueError(f'Expected installed Python source for module: {name}')
        modules.append(name)
    # Custom MCP model methods must import correctly in both Python roles.
    from workmate_product.mcp.custom import parse_custom
    custom = parse_custom({'name': 'packaging-probe', 'url': 'https://tools.example.invalid/mcp',
                           'headers': {'Authorization': 'Bearer packaging-fixture'}})
    card = custom.card()
    assert card.name == 'packaging-probe'
    # Both roles ship the shared ORM and must execute its context defaults.
    check_orm_defaults()
    check_operational_cli(role)
    if role == 'gateway':
        from app.main import app
        assert app.openapi()['paths']
        import httpx
        async def request_routes():
            # ASGITransport skips lifespan, so no database or background worker is started.
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url='http://packaging.test') as client:
                response = await client.get('/health')
                assert response.status_code == 503
                assert response.json() == {'role': 'product-api', 'ready': False}
                assert (await client.get('/openapi.json')).status_code == 200
        asyncio.run(request_routes())
    else:
        from workmate_runtime.delivery.remote_files import _CAPTURE_SCRIPT
        compile(_CAPTURE_SCRIPT, '<capture>', 'exec')
        from io import BytesIO
        from zipfile import ZipFile
        from workmate_runtime.mcp.bundle import build_gateway_bundle
        for remote in (False, True):
            with ZipFile(BytesIO(build_gateway_bundle(remote=remote))) as bundle:
                assert 'workmate_runtime/mcp/http_transport.py' in bundle.namelist()
                for name in bundle.namelist():
                    if name.endswith('.py'):
                        compile(bundle.read(name), name, 'exec')
        import duckdb, openpyxl, pptx, PIL, matplotlib  # noqa: F401
        from matplotlib import font_manager
        font_manager.findfont('Noto Sans CJK SC', fallback_to_default=False)
    packages = sorted(({'name': d.metadata['Name'], 'version': d.version,
                        'license_expression': d.metadata.get('License-Expression'),
                        'license': d.metadata.get('License')}
                       for d in importlib.metadata.distributions()), key=lambda d: d['name'])
    print(json.dumps({'modules': len(modules), 'packages': packages}))


if __name__ == '__main__':
    main(sys.argv[1])
