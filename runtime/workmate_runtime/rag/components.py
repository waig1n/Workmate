"""Isolated knowledge/indexing components; never starts AgentScope App execution.

AgentScope auxiliary storage, collection manager and blob adapters remain pinned
while Worker commands, Replies and Session ownership use workmate transport only.
"""
import os
from agentscope.message import TextBlock
from agentscope.rag import ParserBase

class NonEmptyParser(ParserBase):
    """Keep upstream parsing and reject an otherwise-successful empty index."""

    def __init__(self, parser):
        self.parser = parser
        self.supported_media_types = parser.supported_media_types

    def supported_extensions(self):
        return self.parser.supported_extensions()

    async def parse(self, file, filename):
        sections = await self.parser.parse(file, filename)
        sections = [
            s
            for s in sections
            if isinstance(s.content, TextBlock) and s.content.text.strip()
        ]
        if not sections:
            raise ValueError("No extractable text; scanned documents require OCR")
        return sections


async def refresh_embedding_credential(settings, storage, owner_id, kb_id):
    """Rotate only keys of credentials still owned by this deployment profile.

    Existing endpoint/model/dimensions stay pinned; changing the deployment
    profile cannot silently redirect an old collection to another provider.
    """
    from agentscope.credential import CredentialFactory

    record = await storage.get_knowledge_base(owner_id, kb_id)
    if record is None:
        return
    config = record.data.embedding_model_config
    credential_record = await storage.get_credential(owner_id, config.credential_id)
    if credential_record is None:
        return
    data = credential_record.data
    expected_type = settings.knowledge_embedding_model_type + "_credential"
    endpoint = (data.get("base_url") or "").rstrip("/")
    current_endpoint = (settings.knowledge_embedding_model_base_url or "").rstrip("/")
    if data.get("type") != expected_type or endpoint != current_endpoint:
        return
    key = os.getenv(settings.knowledge_embedding_model_api_key_env, "")
    if not key:
        raise RuntimeError("Knowledge embedding credential is unavailable")
    if data.get("api_key") != key:
        credential = CredentialFactory.from_dict({**data, "api_key": key})
        await storage.upsert_credential(owner_id, credential)


async def apply_embedding_request_policy(settings, storage, owner_id, kb_id, knowledge):
    """Apply the OpenAI wire option without changing the index dimensions.

    AgentScope 2.0.7 exposes pass_dimensions as a constructor option, but its
    service builder does not forward persisted parameters to that option.
    Explicit policies remain fixed. Legacy records may inherit the deployment
    compatibility option only when the complete pinned identity still matches.
    """
    record = await storage.get_knowledge_base(owner_id, kb_id)
    if record is None:
        return
    config = record.data.embedding_model_config
    if config.type != "openai_credential":
        return
    if "pass_dimensions" in config.parameters:
        policy = config.parameters["pass_dimensions"]
        if not isinstance(policy, bool):
            raise ValueError("Invalid persisted knowledge embedding request policy")
    else:
        if (
            settings.knowledge_embedding_model_type != "openai"
            or config.model != settings.knowledge_embedding_model_name
            or config.dimensions != settings.knowledge_embedding_model_dimensions
        ):
            return
        credential = await storage.get_credential(owner_id, config.credential_id)
        if credential is None:
            return
        endpoint = (credential.data.get("base_url") or "").rstrip("/")
        expected = (settings.knowledge_embedding_model_base_url or "").rstrip("/")
        if credential.data.get("type") != "openai_credential" or endpoint != expected:
            return
        policy = settings.knowledge_embedding_model_pass_dimensions
    # Public accessor; the pinned model/collection dimensions remain untouched.
    knowledge.embedding_model.pass_dimensions = policy


def build_knowledge_components(settings, storage):
    if not settings.knowledge.enabled:
        return {}
    if not settings.knowledge_qdrant_api_key.strip():
        raise ValueError("KNOWLEDGE_QDRANT_API_KEY is required for knowledge isolation")
    from agentscope.rag import (
        QdrantStore,
        TextParser,
        PDFParser,
        WordParser,
        PPTParser,
        ExcelParser,
    )
    from agentscope.app.rag.knowledge_base_manager import CollectionPerKbManager
    from agentscope.app.rag.blob_store import S3BlobStore
    from botocore.config import Config

    class RotatingCredentialManager(CollectionPerKbManager):
        async def get_knowledge(self, user_id, knowledge_base_id):
            await refresh_embedding_credential(
                settings, storage, user_id, knowledge_base_id
            )
            knowledge = await super().get_knowledge(user_id, knowledge_base_id)
            await apply_embedding_request_policy(
                settings, storage, user_id, knowledge_base_id, knowledge
            )
            from ..embedding_policy import DimensionCheckedEmbedding
            # AgentScope exposes a read-only embedding_model accessor; this
            # localized assignment wraps the constructed handle before use.
            knowledge._embedding_model = DimensionCheckedEmbedding(knowledge.embedding_model)
            return knowledge

    return dict(
        knowledge_base_manager=RotatingCredentialManager(
            storage,
            QdrantStore(
                url=settings.knowledge_qdrant_url,
                api_key=settings.knowledge_qdrant_api_key,
            ),
        ),
        blob_store=S3BlobStore(
            settings.knowledge_s3_bucket,
            endpoint_url=settings.knowledge_s3_endpoint,
            aws_access_key_id=settings.knowledge_s3_access_key,
            aws_secret_access_key=settings.knowledge_s3_secret_key,
            config=Config(s3={"addressing_style": "path"}),
        ),
        knowledge_parsers=[
            NonEmptyParser(parser)
            for parser in (
                TextParser(),
                PDFParser(),
                WordParser(),
                PPTParser(),
                ExcelParser(separate_sheet=True),
            )
        ],
        enable_index_worker=True,
    )

