"""The Product API validates the same YAML sandbox policy as the Worker."""
import pytest

from app.config_file import WorkmateFileConfig


def test_sandbox_default_and_explicit_policy():
    assert WorkmateFileConfig().workmate_runtime.sandbox.provider == 'bubblewrap'
    config = WorkmateFileConfig.model_validate(
        {'workmate_runtime': {'sandbox': {'provider': 'bubblewrap'}}})
    assert config.workmate_runtime.sandbox.provider == 'bubblewrap'


@pytest.mark.parametrize('sandbox', [{'provider': 'unknown'}, {'provider':'e2b','api_key':'forbidden'},
    {'provider': 'bubblewrap', 'image': 'unused'}, None])
def test_invalid_policy_is_not_ignored(sandbox):
    with pytest.raises(ValueError):
        WorkmateFileConfig.model_validate({'workmate_runtime': {'sandbox': sandbox}})


@pytest.mark.parametrize('provider',['local','bubblewrap','docker','kubernetes','e2b','daytona','opensandbox'])
def test_each_provider_config_is_accepted(provider):
    config=WorkmateFileConfig.model_validate({'workmate_runtime':{'sandbox':{'provider':provider}}})
    assert config.workmate_runtime.sandbox.provider==provider


def test_workspace_lifecycle_uses_shared_strict_policy():
    policy = WorkmateFileConfig().workmate_runtime.workspace_lifecycle
    assert policy.warm_ttl_seconds == 600
    config = WorkmateFileConfig.model_validate({'workmate_runtime': {
        'workspace_lifecycle': {'max_warm_per_worker': 0}}})
    assert config.workmate_runtime.workspace_lifecycle.max_warm_per_worker == 0
    for field, value in [('warm_ttl_seconds', -1), ('sweep_interval_seconds', 0), ('max_warm_per_worker', True)]:
        with pytest.raises(ValueError):
            WorkmateFileConfig.model_validate({'workmate_runtime': {'workspace_lifecycle': {field: value}}})
