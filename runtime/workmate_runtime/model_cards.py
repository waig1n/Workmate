"""Build isolated ModelCards for directly assembled Core models."""
import os
import tempfile
import yaml

_THINKING_OUTPUT_TYPE = "application/x-thinking"


def _profile_input_types(profile) -> list[str]:  # noqa: ANN001
    """Return one profile's validated formatter media types."""
    if profile is None:
        return ["text/plain"]
    return list(profile.model_input_types)


def _profile_output_types(profile) -> list[str]:  # noqa: ANN001
    """Expand one profile's thinking bit into card output media types."""
    types = ["text/plain"]
    if profile is not None and profile.supports_thinking:
        types.append(_THINKING_OUTPUT_TYPE)
    return types


def _synthetic_card_yaml(profile) -> str:  # noqa: ANN001
    """Render one profile as a ModelCard YAML document.

    ``ModelCard.from_yaml`` is the single place AgentScope derives the
    operator-facing parameter schema (hiding thinking parameters and the
    omni-only ``voice`` field, capping ``max_tokens``), so the card is
    materialized through it rather than constructed directly.
    """
    card = {
        "name": profile.model,
        "label": profile.model,
        "status": "active",
        "input_types": _profile_input_types(profile),
        "output_types": _profile_output_types(profile),
        "context_size": profile.context_size,
        "output_size": profile.max_output_tokens,
    }
    return yaml.safe_dump(card, sort_keys=False)


def _build_card(parameter_class, card_yaml: str):
    from agentscope.model import ModelCard

    fd, path = tempfile.mkstemp(suffix=".yaml", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(card_yaml)
        return ModelCard.from_yaml(
            yaml_path=path,
            parameter_class=parameter_class,
        )
    finally:
        os.unlink(path)
