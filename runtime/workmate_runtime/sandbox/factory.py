"""Explicit provider selection; no dynamic imports or fallback."""
from workmate_product.config.sandbox import SandboxSettings, parse_sandbox_settings

from .bubblewrap import BubblewrapSandboxProvider


def create_sandbox_provider(settings: SandboxSettings, *, workspace_factory=None):
    # Validate here too: direct composition must not bypass startup validation.
    policy = parse_sandbox_settings(settings)
    if policy.provider == 'bubblewrap':
        options = {} if workspace_factory is None else {'workspace_factory': workspace_factory}
        return BubblewrapSandboxProvider(**options)
    if workspace_factory is not None:
        raise ValueError('A workspace factory override is supported only for Bubblewrap')
    if policy.provider == 'local':
        from .local import LocalSandboxProvider
        return LocalSandboxProvider()
    from .remote import RemoteSandboxProvider
    return RemoteSandboxProvider(policy)
