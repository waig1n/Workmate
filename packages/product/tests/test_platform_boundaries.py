"""Platform reads and writes share authority without depending on each other."""
import ast
from pathlib import Path

APPLICATION = Path(__file__).resolve().parents[1] / 'src/workmate_product/application'


def test_platform_entrypoints_import_shared_request_and_public_authority():
    for name in ('platform_reads', 'platform_tools'):
        tree = ast.parse((APPLICATION / f'{name}.py').read_text())
        imports = {(node.module, alias.name) for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom) for alias in node.names}
        assert ('platform_types', 'PlatformToolRequest') in imports
        assert ('platform_authority', 'authorize_platform_call') in imports
        assert not any(module == 'platform_tools' for module, _ in imports)
        assert not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and node.name == '_authority' for node in tree.body)


def test_platform_request_and_authority_have_shared_ownership():
    from workmate_product.application.platform_types import PlatformToolRequest
    from workmate_product.application.platform_authority import authorize_platform_call
    from workmate_product.application import platform_reads, platform_tools
    assert PlatformToolRequest.__module__.endswith('.platform_types')
    assert platform_reads.authorize_platform_call is authorize_platform_call
    assert platform_tools.authorize_platform_call is authorize_platform_call
    assert not hasattr(platform_tools, '_authority')
