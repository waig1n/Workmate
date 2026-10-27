# -*- coding: utf-8 -*-
"""Shared view builder for capability list items (skills & mcps)."""

from workmate_product.persistence.models import CapabilityInstallation, Scope
from ..schemas import CapabilityListItem


def capability_item(
    installation: CapabilityInstallation,
    manageable: bool,
    usable: bool,
    scope: Scope | None,
) -> CapabilityListItem:
    return CapabilityListItem(
        id=installation.id,
        capability_type=installation.capability_type,
        scope_id=installation.scope_id,
        name=installation.name,
        display_name=installation.display_name,
        enabled=installation.enabled,
        metadata_=installation.metadata_,
        manageable=manageable,
        usable=usable,
        scope_kind=scope.kind if scope else None,
    )
