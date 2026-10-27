import pytest

from app.api.schemas import AgentCreate, AgentUpdate


@pytest.mark.parametrize("extra", [{}, {"description": ""}, {"description": "  \n "}])
def test_creation_supplies_public_description_without_exposing_prompt(extra):
    agent = AgentCreate(
        slug="report-helper", display_name="Report assistant",
        system_prompt="INTERNAL SECRET", **extra,
    )
    assert agent.description.strip()
    assert "Report assistant" in agent.description
    assert "INTERNAL SECRET" not in agent.description


def test_creation_preserves_explicit_description_and_partial_updates():
    agent = AgentCreate(
        slug="report-helper", display_name="Report assistant", description="Prepare weekly reports.",
    )
    assert agent.description == "Prepare weekly reports."
    assert AgentUpdate(display_name="New name").description is None


@pytest.mark.asyncio
async def test_created_description_round_trips_through_api(client_and_app, auth_headers):
    http, _ = client_and_app
    created = await http.post("/api/agents", headers=auth_headers, json={
        "slug": "description-test", "display_name": "Report assistant",
        "system_prompt": "INTERNAL SECRET", "description": "  ",
    })
    assert created.status_code == 201, created.text
    detail = await http.get(f"/api/agents/{created.json()['id']}", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["description"] == created.json()["description"]
    assert "Report assistant" in detail.json()["description"]
    assert "INTERNAL SECRET" not in detail.json()["description"]
