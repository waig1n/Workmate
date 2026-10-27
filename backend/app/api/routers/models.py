# -*- coding: utf-8 -*-
"""Model profile listing API.

Exposes the chat model profiles declared in workmate.yaml so the frontend
can offer per-conversation model selection. Capabilities are derived from
validated media presets; only non-secret fields are returned.
"""

import os

from fastapi import APIRouter, Depends

from ...auth import get_current_user
from ...config_file import models_file_config
from ..schemas import ModelProfileOut

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=list[ModelProfileOut])
async def list_models(
    user: object = Depends(get_current_user),
) -> list[ModelProfileOut]:
    """List selectable chat model profiles with capability metadata.

    Physical deployment details (provider model id, provider type,
    context size) are intentionally omitted — users see product
    profiles, not vendor internals.
    """
    del user  # Any authenticated user may list profiles.
    models = models_file_config()
    profiles = models.profiles
    default_name = models.default
    return [
        ModelProfileOut(
            id=name,
            display_name=name,
            media_profile=profile.media_profile,
            supports_multimodal_input=(
                any(
                    value != "text" for value in
                    models.media_profiles[
                        profile.media_profile
                    ].model_inputs
                )
                or models.media_profiles[
                    profile.media_profile
                ].pdf_strategy == "native"
            ),
            supports_images=(
                "image" in
                models.media_profiles[profile.media_profile].model_inputs
            ),
            supports_audio=(
                "audio" in
                models.media_profiles[profile.media_profile].model_inputs
            ),
            supports_video=(
                "video" in
                models.media_profiles[profile.media_profile].model_inputs
            ),
            # Both strategies accept PDFs: extract_text performs local
            # extraction; native forwards a PDF block to the provider.
            supports_documents=True,
            supports_native_pdf=(
                models.media_profiles[
                    profile.media_profile
                ].pdf_strategy == "native"
            ),
            supports_thinking=profile.supports_thinking,
            is_default=name == default_name,
            available=bool(
                os.getenv(profile.api_key_env, "")
            ),
        )
        for name, profile in sorted(profiles.items())
    ]
