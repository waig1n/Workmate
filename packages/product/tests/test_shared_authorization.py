"""The Worker imports one ORM and authorization service without the Web layer."""
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]


def run_python(source):
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join([
        str(ROOT / 'packages/product/src'), str(ROOT / 'packages/contracts/src'),
        str(ROOT / 'backend'),
    ])}
    result = subprocess.run([sys.executable, '-c', source], env=env, capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_shared_authorization_imports_with_web_and_engine_imports_blocked():
    run_python('''
import sys
from importlib.abc import MetaPathFinder
class Reject(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'fastapi','agentscope','app','backend'}:
            raise AssertionError('Forbidden dependency: '+fullname)
sys.meta_path.insert(0, Reject())
from workmate_product.persistence.models import Base, Agent, RuntimeSession
from workmate_product.authorization.permissions import ALL_PERMISSIONS
from workmate_product.application.authorization import AccessDecisionService
from workmate_product.application.knowledge_access import validate_knowledge_bindings
from workmate_product.config.knowledge import KnowledgeConfig
from sqlalchemy.orm import configure_mappers
configure_mappers()
assert Agent.metadata is Base.metadata
assert RuntimeSession.metadata is Base.metadata
assert 'agent.use' in ALL_PERMISSIONS
assert AccessDecisionService(None)._db is None
''')


def test_api_authorization_uses_shared_permissions_and_one_metadata():
    run_python('''
from workmate_product.persistence import models as shared
from workmate_product.authorization import permissions as keys
from workmate_product.application.authorization import AccessDecisionService
from app.auth import permissions as old_keys
from app.services.access_decision import AccessDecisionService as old_service
assert issubclass(old_service, AccessDecisionService)
for name, value in vars(keys).items():
    if name.isupper():
        assert getattr(old_keys,name) is value, name
from sqlalchemy.orm import configure_mappers
configure_mappers()
assert len(shared.Base.metadata.tables) == len(set(shared.Base.metadata.tables))
''')


def test_shared_knowledge_default_is_closed_and_parameters_remain_strict():
    run_python("""
import asyncio
from types import SimpleNamespace
from workmate_product.application.authorization import AccessDecisionService
from workmate_product.application.knowledge_access import validate_knowledge_bindings, KnowledgeBindingError
from workmate_product.config.knowledge import KnowledgeConfig
from app.api.knowledge_schemas import KnowledgeConfig as ApiConfig
assert ApiConfig is KnowledgeConfig
async def check():
    try:
        await validate_knowledge_bindings(None,'user',SimpleNamespace(scope_id='scope'),
            {'knowledge_base_ids':['kb']},enabled=False,access=None)
    except KnowledgeBindingError as error:
        assert error.code=='knowledge_disabled' and error.status_code==503
    else:
        raise AssertionError('Missing policy did not fail closed')
asyncio.run(check())
""")


def test_resource_applications_import_without_web_or_runtime():
    run_python('''
import sys
from importlib.abc import MetaPathFinder
class Reject(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'fastapi', 'agentscope', 'app', 'backend', 'workmate_runtime'}:
            raise AssertionError('Forbidden dependency: ' + fullname)
sys.meta_path.insert(0, Reject())
from workmate_product.application import resource_lists, catalog_status
''')


def test_resource_repositories_do_not_import_application_or_web():
    run_python('''
import sys
from importlib.abc import MetaPathFinder
class Reject(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if (fullname.split('.')[0] in {'fastapi', 'agentscope', 'app', 'backend', 'workmate_runtime'}
                or fullname == 'workmate_product.application'
                or fullname.startswith('workmate_product.application.')):
            raise AssertionError('Forbidden dependency: ' + fullname)
sys.meta_path.insert(0, Reject())
from workmate_product.persistence.repositories import resource_lists, catalog_status
''')
