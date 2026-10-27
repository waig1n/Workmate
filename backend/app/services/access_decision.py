"""API authorization adapter injecting the current deployment knowledge policy."""
from workmate_product.application.authorization import (
    AccessDecisionService as SharedAccessDecisionService,
    _MANAGEMENT_PERMISSIONS,  # noqa: F401
)


def _knowledge_enabled():
    # Lazy import avoids the existing KnowledgeService/authorization cycle.
    from .knowledge import knowledge_settings
    return knowledge_settings().enabled


class AccessDecisionService(SharedAccessDecisionService):
    def __init__(self, db):
        super().__init__(db, knowledge_enabled=_knowledge_enabled)
