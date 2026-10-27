"""Catalog visibility and scope authorization, independent of HTTP transport."""
from ..persistence.models import CapabilityType, ScopeKind
from ..persistence.repositories import catalog_status as repository


class CatalogAccessError(ValueError):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def annotate(db, access, user_id, page, hub_id, kind, *, scope_id=None):
    """One EXISTS projection per card, in one SQL statement for this page."""
    if kind == CapabilityType.MCP and scope_id is None:
        return page
    can_manage = False
    if kind == CapabilityType.SKILL:
        scope = await repository.catalog_scope(db, kind, None)
        scope_id = scope.id if scope else None
        if scope_id is not None:
            can_manage = await access.has_permission(user_id, scope_id, 'skill.manage')
    else:
        scope = await repository.catalog_scope(db, kind, scope_id)
        if scope is None or scope.kind == ScopeKind.PERSONAL:
            raise CatalogAccessError(404, 'Organization scope not found')
        can_manage = await access.has_permission(user_id, scope_id, 'mcp.manage')
        if not can_manage:
            raise CatalogAccessError(403, 'Permission required in this scope: mcp.manage')
    cards = page['cards']
    if not cards or scope_id is None:
        return {**page, 'cards': [{**card, 'installed': False} for card in cards]}
    installed = await repository.installed_flags(db, scope_id=scope_id, kind=kind,
        user_id=user_id, can_manage=can_manage, hub_id=hub_id, cards=cards)
    return {**page, 'cards': [{**card, 'installed': bool(flag)}
                            for card, flag in zip(cards, installed, strict=True)]}
