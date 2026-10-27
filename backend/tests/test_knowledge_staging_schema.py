"""Current model and deployment contracts."""
from sqlalchemy.orm import configure_mappers


def test_staged_upload_model_is_registered_and_exported():
    from workmate_product.persistence.models import Base, KnowledgeDocumentUpload
    assert Base.metadata.tables['knowledge_document_uploads'] is KnowledgeDocumentUpload.__table__
