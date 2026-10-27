"""Public document status projection without execution dependencies."""
def document_projection(record):
    value = record.model_dump(mode="json")
    return {
        **{k: value[k] for k in ("id", "status", "created_at", "updated_at")},
        **{
            k: value["data"][k]
            for k in ("filename", "size", "content_type", "chunk_count")
        },
        "error": (
            "No extractable text; scanned documents require OCR"
            if record.data.error and "No extractable text" in record.data.error
            else "Embedding request rejected; check model parameters and provider compatibility"
            if record.data.error and record.data.error.startswith("BadRequestError:")
            else "Document indexing failed; inspect the service diagnostics and retry"
            if record.data.error
            else None
        ),
    }
