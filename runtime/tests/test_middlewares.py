# -*- coding: utf-8 -*-
"""Tests for workmate runtime middleware wiring."""

import os
from workmate_runtime.config import RuntimeSettings
from workmate_runtime.middlewares import (
    build_embedding_model_from_settings,
    reme_workspace_dir,
)


def _embedding_settings(dimensions: int | None = None) -> RuntimeSettings:
    values = {
        "embedding_model_type": "openai",
        "embedding_model_name": "BAAI/bge-m3",
        "embedding_model_api_key_env": "EMBEDDING_MODEL_API_KEY",
    }
    if dimensions is not None:
        values["embedding_model_dimensions"] = dimensions
    return RuntimeSettings(**values)


def test_reme_workspace_is_isolated_by_user_and_agent(tmp_path):
    root = str(tmp_path / "reme")

    first = reme_workspace_dir(root, "user-1", "agent-1")
    other_user = reme_workspace_dir(root, "user-2", "agent-1")
    other_agent = reme_workspace_dir(root, "user-1", "agent-2")

    assert first != other_user
    assert first != other_agent
    assert os.path.commonpath([root, first]) == root


def test_reme_workspace_identity_cannot_escape_root(tmp_path):
    root = str(tmp_path / "reme")
    path = reme_workspace_dir(root, "../../another-user", "../agent")

    assert os.path.commonpath([root, path]) == root
    assert ".." not in os.path.relpath(path, root).split(os.sep)


def test_openai_embedding_dimensions_default(monkeypatch):
    monkeypatch.setenv("EMBEDDING_MODEL_API_KEY", "test")

    model = build_embedding_model_from_settings(_embedding_settings())

    assert model is not None
    assert model.dimensions == 1024


class _ClosableReMeApp:
    def __init__(self):
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1
