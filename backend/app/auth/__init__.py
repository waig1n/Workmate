# -*- coding: utf-8 -*-
"""Auth package."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .deps import (
        get_access_decision,
        get_current_user,
        get_current_user_id,
        require_root_admin,
        require_root_role_binder,
        require_user_reader, require_user_manager, require_role_reader, require_role_manager,
    )
    from .security import (
        create_access_token,
        decode_access_token,
        hash_password,
        normalize_email,
        verify_password,
    )

__all__ = [
    "get_access_decision",
    "get_current_user",
    "get_current_user_id",
    "require_root_admin",
    "require_root_role_binder",
    "require_user_reader", "require_user_manager", "require_role_reader", "require_role_manager",
    "create_access_token",
    "decode_access_token",
    "hash_password",
    "normalize_email",
    "verify_password",
]

_EXPORT_MODULES = {
    "get_access_decision": ".deps",
    "get_current_user": ".deps",
    "get_current_user_id": ".deps",
    "require_root_admin": ".deps",
    "require_root_role_binder": ".deps",
    "require_user_reader": ".deps",
    "require_user_manager": ".deps",
    "require_role_reader": ".deps",
    "require_role_manager": ".deps",
    "create_access_token": ".security",
    "decode_access_token": ".security",
    "hash_password": ".security",
    "normalize_email": ".security",
    "verify_password": ".security",
}


def __getattr__(name: str) -> Any:
    """Load public auth helpers without eager service-layer imports."""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Expose lazy public names without importing their modules."""
    return sorted(set(globals()) | set(__all__))
