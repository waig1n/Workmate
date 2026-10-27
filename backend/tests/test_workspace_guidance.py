
import pytest
from pydantic import ValidationError

from app.prompts import compose_system_prompt, WORKSPACE_GOVERNANCE_PROMPT
from app.api.schemas import AgentCreate, AgentUpdate


def test_existing_custom_prompt_retained_with_current_guidance():
    prompt = compose_system_prompt("Administrator instructions", workspace=True)
    assert prompt.startswith("Administrator instructions")
    assert WORKSPACE_GOVERNANCE_PROMPT in prompt
    assert compose_system_prompt(prompt, workspace=True) == prompt
    assert WORKSPACE_GOVERNANCE_PROMPT not in compose_system_prompt("Other agent")


def test_description_is_separate_and_bounded():
    agent = AgentCreate(slug="test-agent", display_name="Test", description="Public introduction")
    assert agent.description == "Public introduction"
    assert agent.system_prompt == ""
    assert AgentUpdate(description="").description == ""
    with pytest.raises(ValidationError):
        AgentUpdate(description="x" * 2001)


def test_language_guidance_applies_to_all_agents_and_is_idempotent():
    from app.prompts import RESPONSE_LANGUAGE_PROMPT, finalize_system_prompt
    prompt = compose_system_prompt("Custom instructions")
    assert RESPONSE_LANGUAGE_PROMPT in prompt
    assert compose_system_prompt(prompt) == prompt

    effective = finalize_system_prompt(prompt, "English runtime workspace instructions")
    assert effective.endswith(RESPONSE_LANGUAGE_PROMPT.strip())
    assert effective.count(RESPONSE_LANGUAGE_PROMPT.strip()) == 1
    assert effective.index("English runtime workspace instructions") < effective.index("## Response language")
