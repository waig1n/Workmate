"""Resolve native handles through an isolated knowledge manager, never ChatService.

The manager owns fixed embedding/collection descriptors and its lifecycle. Product
identity mappings and authorization remain checked in this Worker adapter.
"""
import asyncio
from sqlalchemy import select
from workmate_product.persistence.models import KnowledgeBase
from .middleware import WorkerRAGMiddleware, KnowledgeSearchDenied


class WorkerKnowledgeResolver:
    def __init__(self, sessions, manager, *, timeout):
        self.sessions, self.manager, self.timeout = sessions, manager, timeout

    async def _bindings(self, ids):
        async with self.sessions.begin() as db:
            rows = (await db.scalars(select(KnowledgeBase).where(KnowledgeBase.id.in_(ids)))).all()
            if {row.id for row in rows} != set(ids) or any(not row.enabled for row in rows):
                raise KnowledgeSearchDenied('Bound knowledge is unavailable')
            return {row.id: (row.runtime_owner_id, row.runtime_id) for row in rows}

    async def prepare(self, context, authorize, *, authorize_preparation):
        config = context.knowledge_config.model_copy(deep=True)
        ids = config.knowledge_base_ids
        if not ids:
            return None
        await authorize_preparation()
        bindings = await self._bindings(ids)

        async def current():
            await authorize()
            if await self._bindings(ids) != bindings:
                raise KnowledgeSearchDenied('Knowledge identity mapping changed')

        # Do not catch and skip an unavailable KB: partial assembly can expose
        # an answer that incorrectly appears to have searched every bound base.
        async with asyncio.timeout(self.timeout):
            handles = {key: await self.manager.get_knowledge(owner, runtime_id)
                       for key, (owner, runtime_id) in bindings.items()}
        await authorize_preparation()
        if await self._bindings(ids) != bindings:
            raise KnowledgeSearchDenied('Knowledge identity mapping changed during preparation')
        return WorkerRAGMiddleware(handles, config, authorize=current, timeout=self.timeout)
