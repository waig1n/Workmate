"""Build the finite gateway package shipped to sandbox Python interpreters.

Only gateway-side modules are included: no Worker, Product, or configuration
state. Dependencies remain supplied by the existing sandbox bootstrap.
"""
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile


_COMMON_MODULES = ('actor', 'http_transport', 'server')
_REMOTE_MODULES = ('remote_wire', 'remote_server')


def build_gateway_bundle(*, remote: bool = False) -> bytes:
    source = Path(__file__).parent
    entry = 'remote_server' if remote else 'server'
    output = BytesIO()
    with ZipFile(output, 'w') as archive:
        archive.writestr('workmate_runtime/__init__.py', '')
        archive.writestr('workmate_runtime/mcp/__init__.py', '')
        for module in _COMMON_MODULES + (_REMOTE_MODULES if remote else ()):
            archive.writestr(f'workmate_runtime/mcp/{module}.py',
                             (source / f'{module}.py').read_bytes())
        archive.writestr('__main__.py',
                         f'from workmate_runtime.mcp.{entry} import main\nmain()\n')
    return output.getvalue()
