# -*- coding: utf-8 -*-
"""Per-conversation model selection API and credential routing tests."""

import pytest
from types import SimpleNamespace

from workmate_product.persistence.models import Agent, Conversation, ModelCredential, Scope, User

pytestmark = pytest.mark.anyio

# The repository workmate.yaml declares these profiles (mirrored by the
# contract tests); tests assert against them rather than fixtures.
TEXT_PROFILE = "deepseek-v4-flash"
VLM_PROFILE = "kimi-2.6"


async def test_list_models_exposes_profiles(client_and_app, auth_headers):
    http, _ = client_and_app
    resp = await http.get("/api/models", headers=auth_headers)
    assert resp.status_code == 200
    profiles = {p["id"]: p for p in resp.json()}
    assert set(profiles) == {TEXT_PROFILE, VLM_PROFILE}
    assert profiles[TEXT_PROFILE]["is_default"] is True
    assert profiles[VLM_PROFILE]["is_default"] is False
    assert profiles[VLM_PROFILE]["supports_multimodal_input"] is True
    assert profiles[TEXT_PROFILE]["supports_multimodal_input"] is False
    assert profiles[VLM_PROFILE]["media_profile"] == "vision-documents"
    assert profiles[VLM_PROFILE]["supports_images"] is True
    assert profiles[VLM_PROFILE]["supports_audio"] is False
    assert profiles[VLM_PROFILE]["supports_video"] is False
    assert profiles[VLM_PROFILE]["supports_documents"] is True
    assert profiles[VLM_PROFILE]["supports_native_pdf"] is False
    assert profiles[VLM_PROFILE]["available"] is True
    assert profiles[TEXT_PROFILE]["display_name"] == TEXT_PROFILE
    # Product-facing listing: no secrets AND no physical deployment
    # details (provider model id, provider type, context size).
    forbidden = {"api_key", "model", "provider", "context_size",
                 "base_url", "api_key_env"}
    for p_ in profiles.values():
        assert not (forbidden & set(p_))


async def test_create_conversation_with_model_profile(
    client_and_app, auth_headers, seeded_ids,
):
    http, _ = client_and_app
    agent_id, _ = seeded_ids
    resp = await http.post("/api/conversations", json={
        "agent_id": agent_id, "title": "VLM work",
        "model_profile": VLM_PROFILE,
    }, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["model_profile"] == VLM_PROFILE


async def test_create_conversation_unknown_profile_rejected(
    client_and_app, auth_headers, seeded_ids,
):
    http, _ = client_and_app
    agent_id, _ = seeded_ids
    resp = await http.post("/api/conversations", json={
        "agent_id": agent_id, "title": "Bad model",
        "model_profile": "no-such-model",
    }, headers=auth_headers)
    assert resp.status_code == 422
    assert "no-such-model" in resp.json()["detail"]


async def test_switch_to_null_snapshots_current_default(
    client_and_app, auth_headers, fake_client, db_session, seeded_ids,
):
    """model_profile: null snapshots the current default profile name, so
    a later default change cannot rewrite what this conversation runs."""
    http, _ = client_and_app
    _, conv_id = seeded_ids
    resp = await http.patch(
        f"/api/conversations/{conv_id}/model",
        json={"model_profile": None},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["model_profile"] == TEXT_PROFILE

    # Re-switching to the same snapshot value also stays a no-op.
    resp = await http.patch(
        f"/api/conversations/{conv_id}/model",
        json={"model_profile": TEXT_PROFILE},
        headers=auth_headers,
    )
    assert resp.status_code == 200


async def test_switch_model_unknown_profile_rejected(
    client_and_app, auth_headers, seeded_ids,
):
    http, _ = client_and_app
    _, conv_id = seeded_ids
    resp = await http.patch(
        f"/api/conversations/{conv_id}/model",
        json={"model_profile": "no-such-model"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


async def _user_id(http, auth_headers) -> str:
    me = await http.get("/api/auth/me", headers=auth_headers)
    assert me.status_code == 200
    return me.json()["id"]


async def test_switch_model_denied_without_agent_use_right(
    client_and_app, auth_headers, fake_client, db_session, seeded_ids,
    monkeypatch,
):
    """A user who lost the agent's use right cannot switch models via the
    API even though the conversation is still theirs (frontend disabling
    is never the gate)."""
    from workmate_product.application.authorization import AccessDecisionService

    async def deny(self, user_id, agent_id):
        return False

    monkeypatch.setattr(AccessDecisionService, "can_use_agent", deny)

    http, _ = client_and_app
    _, conv_id = seeded_ids
    resp = await http.patch(
        f"/api/conversations/{conv_id}/model",
        json={"model_profile": VLM_PROFILE},
        headers=auth_headers,
    )
    assert resp.status_code == 403


async def test_switch_model_rejected_on_unavailable_capabilities(
    client_and_app, auth_headers, fake_client, db_session, seeded_ids,
    monkeypatch,
):
    from workmate_product.application.authorization import AccessDecisionService

    async def allow(self, user_id, agent_id):
        return True

    async def unavailable(self, user_id, agent_id):
        return ["lost-skill-id"]

    monkeypatch.setattr(AccessDecisionService, "can_use_agent", allow)
    monkeypatch.setattr(
        AccessDecisionService, "unavailable_capability_ids", unavailable,
    )

    http, _ = client_and_app
    _, conv_id = seeded_ids
    resp = await http.patch(
        f"/api/conversations/{conv_id}/model",
        json={"model_profile": VLM_PROFILE},
        headers=auth_headers,
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "unavailable_capabilities"
