"""Stable Product knowledge-source projection, independent of App services."""
import hashlib
from agentscope.message import TextBlock


def source_projection(product_id, hit):
    chunk = hit.chunk
    token = hashlib.sha256(
        f"{product_id}:{hit.document_id}:{chunk.chunk_index}".encode()
    ).hexdigest()[:24]
    return dict(
        source_id=f"kb-{token}",
        knowledge_base_id=product_id,
        document_id=hit.document_id,
        filename=chunk.source,
        chunk_index=chunk.chunk_index,
        metadata={
            k: v for k, v in chunk.metadata.items() if k in {"page", "slide", "sheet"}
        },
        excerpt=chunk.content.text[:8000]
        if isinstance(chunk.content, TextBlock)
        else "",
        score=hit.score,
    )
