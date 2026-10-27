"""Knowledge-only embedding wire compatibility and vector shape checks."""

from urllib.parse import urlsplit


def resolve_dimensions_policy(
    *,
    provider,
    model,
    base_url,
    dimensions,
    mode="auto",
):
    """Resolve a wire option from verified capabilities or explicit intent.

    Unknown providers never inherit official OpenAI capabilities merely by
    offering an OpenAI-compatible route. Dimensions describe the stored index,
    independent of whether they appear in an HTTP request.
    """
    if mode not in {"auto", "send", "omit"}:
        raise ValueError("Invalid knowledge dimensions_mode")
    if (
        isinstance(dimensions, bool)
        or not isinstance(dimensions, int)
        or dimensions <= 0
    ):
        raise ValueError("Knowledge embedding dimensions must be positive")
    try:
        endpoint = urlsplit(base_url) if base_url else None
        if endpoint and (
            endpoint.scheme not in {"http", "https"}
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
        ):
            raise ValueError("Invalid knowledge embedding endpoint")
        host = endpoint.hostname if endpoint else "api.openai.com"
    except ValueError as exc:
        raise ValueError("Invalid knowledge embedding endpoint") from exc
    official = provider == "openai" and host == "api.openai.com"
    silicon = (
        provider == "openai"
        and host in {"api.siliconflow.cn", "api.siliconflow.com"}
        and model == "BAAI/bge-m3"
    )
    limits = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}
    if official and model in limits and dimensions > limits[model]:
        raise ValueError(
            "Knowledge dimensions exceed the official embedding model limit"
        )
    if silicon and dimensions != 1024:
        raise ValueError("SiliconFlow BAAI/bge-m3 requires 1024 dimensions")
    if mode == "send":
        result = True
    elif mode == "omit":
        result = False
    elif official and model in limits:
        result = True
    elif silicon:
        result = False
    else:
        raise ValueError(
            "Unknown embedding capability; select an explicit dimensions_mode send or omit"
        )
    if silicon and result:
        raise ValueError("SiliconFlow BAAI/bge-m3 does not support sending dimensions")
    if official and model in limits and not result and dimensions != limits[model]:
        raise ValueError(
            "Omitting dimensions requires the official model default dimension"
        )
    if provider == "dashscope" and not result:
        raise ValueError(
            "DashScope adapter cannot omit dimension; select explicit send"
        )
    return result


class DimensionCheckedEmbedding:
    """Delegate to the pinned model and reject malformed vector responses."""

    def __init__(self, model):
        self._model = model

    def __getattr__(self, name):
        return getattr(self._model, name)

    async def __call__(self, inputs, **kwargs):
        response = await self._model(inputs, **kwargs)
        if len(response.embeddings) != len(inputs):
            raise RuntimeError("Embedding response vector count mismatch")
        if any(len(vector) != self._model.dimensions for vector in response.embeddings):
            raise RuntimeError(
                "Embedding response dimension mismatch; vector write rejected"
            )
        return response
