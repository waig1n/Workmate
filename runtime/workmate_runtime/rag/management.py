"""Explicit knowledge resources, with no App or Session execution state."""
import asyncio
from fastapi import HTTPException


class KnowledgeManagement:
    def __init__(self, settings, storage, message_bus, blob_store, service):
        self.settings = settings
        self.storage = storage
        self.message_bus = message_bus
        self.blob_store = blob_store
        self._service = service
        self.mutation_lock = asyncio.Lock()

    @property
    def service(self):
        if not self.settings.knowledge.enabled:
            raise HTTPException(503, "Knowledge service is disabled")
        return self._service
