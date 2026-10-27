# -*- coding: utf-8 -*-
"""Artifact MIME and inline-disposition policy tests."""

from app.artifacts.policy import artifact_mime_type, may_preview_inline
from app.config import Settings


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        artifact_preview_extensions="md,json,png,svg",
        artifact_force_download_extensions="html,svg,zip",
        **overrides,
    )


def test_unknown_file_types_are_deliverable_but_not_inline_previewable():
    settings = _settings()

    assert artifact_mime_type("bundle.custom-format") == (
        "application/octet-stream"
    )
    assert may_preview_inline(
        "bundle.custom-format",
        "application/octet-stream",
        settings,
    ) is False


def test_configured_passive_type_may_be_previewed():
    assert may_preview_inline(
        "report.md", "text/markdown", _settings(),
    ) is True


def test_active_content_is_never_inline_even_if_preview_listed():
    assert may_preview_inline(
        "diagram.svg", "image/svg+xml", _settings(),
    ) is False


def test_force_download_extension_wins_over_passive_mime():
    assert may_preview_inline(
        "archive.zip", "text/plain", _settings(),
    ) is False
