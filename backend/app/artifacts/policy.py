# -*- coding: utf-8 -*-
"""Artifact MIME detection and safe inline-preview policy."""

from __future__ import annotations

import mimetypes
import os

from ..config import Settings

_MIME_OVERRIDES = {
    ".md": "text/markdown",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}

_ACTIVE_CONTENT_MIME_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "image/svg+xml",
    "application/xml",
    "text/xml",
    "application/javascript",
    "text/javascript",
}


def extension_set(raw: str) -> set[str]:
    return {
        f".{item.strip().lower().lstrip('.')}"
        for item in raw.split(",")
        if item.strip()
    }


def artifact_mime_type(name: str) -> str:
    extension = os.path.splitext(name)[1].lower()
    return _MIME_OVERRIDES.get(
        extension,
        mimetypes.guess_type(name)[0] or "application/octet-stream",
    )


def may_preview_inline(
    name: str,
    mime_type: str,
    settings: Settings,
) -> bool:
    """Allow inline rendering only for explicitly configured passive types."""
    extension = os.path.splitext(name)[1].lower()
    if mime_type.lower() in _ACTIVE_CONTENT_MIME_TYPES:
        return False
    if extension in extension_set(settings.artifact_force_download_extensions):
        return False
    return extension in extension_set(settings.artifact_preview_extensions)
