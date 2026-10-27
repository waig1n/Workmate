"""Usage authorization, aggregation and immutable attribution."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config_file import WorkmateFileConfig, UsageFileConfig
from workmate_product.persistence.models import Conversation, Task, User
from workmate_product.persistence.models import TaskUsageScope
from app.services.usage import snapshot_usage_scopes, snapshot_usage_pricing, finish_usage_cost


def test_optional_pricing_validation():
    assert UsageFileConfig().prices == {}
    value = UsageFileConfig(prices={"chat": {"input_per_k": "0.001", "output_per_k": "0"}})
    assert value.prices["chat"].input_per_k == Decimal("0.001")
    for price in ({"input_per_k": -1, "output_per_k": 1}, {"input_per_k": "NaN", "output_per_k": 1}, {"input_per_k": 1}, {"input_per_k": 1, "output_per_k": "Infinity"}):
        with pytest.raises(ValidationError):
            UsageFileConfig(prices={"chat": price})
    with pytest.raises(ValidationError):
        WorkmateFileConfig.model_validate({"product": {"usage": {"prices": {"absent": {"input_per_k": 1, "output_per_k": 2}}}}})


async def test_personal_and_organization_summary(client_and_app, auth_headers, seeded_ids, db_session):
    http, _ = client_and_app
    agent, cid = seeded_ids
    conv = await db_session.get(Conversation, cid)
    other = User(id="other", email="other@test.dev", display_name="Other", password_hash="unused")
    db_session.add(other)
    await db_session.flush()
    db_session.add(Conversation(id="other-conv", user_id=other.id, agent_id=agent))
    await db_session.flush()
    at = datetime(2026, 9, 14, tzinfo=timezone.utc)
    tasks = [Task(id="a", conversation_id=cid, title="a", created_at=at, status="failed", token_usage={"input_tokens": 1000, "output_tokens": 500}, usage_cost=Decimal("0.02"), usage_currency="CNY"), Task(id="b", conversation_id=cid, title="b", created_at=at, status="cancelled", token_usage=None), Task(id="c", conversation_id="other-conv", title="c", created_at=at, status="completed", token_usage={"input_tokens": 9000, "output_tokens": 0})]
    db_session.add_all(tasks)
    await db_session.flush()
    scopes = (await http.get("/api/usage/scopes", headers=auth_headers)).json()
    root = next(s["id"] for s in scopes if s["kind"] == "organization")
    for task in tasks:
        db_session.add(TaskUsageScope(task_id=task.id, scope_id=root))
    await db_session.commit()
    query = "/api/usage?since=2026-09-14&until=2026-09-14"
    response = await http.get(query, headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["task_count"] == 2
    assert body["summary"]["recorded_tasks"] == 1
    assert body["summary"]["input_tokens"] == 1000
    assert Decimal(body["summary"]["costs"][0]["amount"]) == Decimal("0.02")
    assert body["daily"][0]["date"] == "2026-09-14"
    assert (await http.get(query + f"&scope_id={root}", headers=auth_headers)).json()["summary"]["task_count"] == 3
    assert (await http.get(query + "&user_id=other", headers=auth_headers)).status_code == 403
    assert (await http.get(query + "&scope_id=absent", headers=auth_headers)).status_code == 403
    assert (await http.get("/api/usage?since=2024-01-01&until=2026-01-01", headers=auth_headers)).status_code == 422
    assert (await http.get("/api/usage?since=2026-09-15&until=2026-09-14", headers=auth_headers)).status_code == 422


def test_yaml_prices_are_optional_and_cannot_be_overridden_by_env(tmp_path, monkeypatch):
    from app.config_file import load_file_config
    path = tmp_path / "usage.yaml"
    path.write_text('''product:
  models:
    profiles:
      primary:
        type: openai
        model: model-one
  usage:
    currency: USD
    prices:
      primary:
        input_per_k: 0.001
        output_per_k: 0.002
''')
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(path))
    monkeypatch.setenv("USAGE_CURRENCY", "EUR")
    monkeypatch.setenv("USAGE_PRICES", "invalid")
    config = load_file_config()
    assert config.product.usage.currency == "USD"
    task = Task(id="test", conversation_id="conv", title="test")
    conversation = Conversation(id="conv", user_id="u", agent_id="a", model_profile="primary")
    snapshot_usage_pricing(task, conversation)
    conversation.model_profile = "changed"
    task.token_usage = {"input_tokens": 1000, "output_tokens": 2000}
    finish_usage_cost(task)
    assert task.usage_model_profile == "primary"
    assert task.usage_model_id == "model-one"
    assert task.usage_cost == Decimal("0.005")
    assert task.usage_currency == "USD"
    finish_usage_cost(task, model_matches=False)
    assert task.usage_cost is None
    task.token_usage = {"input_tokens": 0, "output_tokens": 0}
    finish_usage_cost(task)
    assert task.usage_cost == 0
    task.token_usage = {"input_tokens": 10}
    finish_usage_cost(task)
    assert task.usage_cost is None
    snapshot_usage_pricing(task, conversation)
    assert task.usage_pricing is None


async def test_department_snapshot_permissions_and_options(client_and_app, auth_headers, seeded_ids, db_session):
    from sqlalchemy import delete, select
    from app.auth.security import create_access_token
    from workmate_product.persistence.models import (
        Permission,
        Role,
        RoleBinding,
        RolePermission,
        ScopeMembership,
        ScopeKind,
    )
    from app.services.bootstrap import create_scope, get_root_scope
    http, _ = client_and_app
    agent, cid = seeded_ids
    conv = await db_session.get(Conversation, cid)
    root = await get_root_scope(db_session)
    dept = await create_scope(db_session, kind=ScopeKind.DEPARTMENT, name="Dept", parent=root)
    sibling = await create_scope(db_session, kind=ScopeKind.DEPARTMENT, name="Sibling", parent=root)
    manager = User(id="manager", email="manager@test.dev", display_name="Manager", password_hash="unused")
    db_session.add(manager)
    await db_session.flush()
    role = Role(id="usage-role", key="usage-reader", display_name="Usage Reader", delegatable=True)
    db_session.add(role)
    await db_session.flush()
    permission = await db_session.scalar(select(Permission).where(Permission.key == "usage.read"))
    db_session.add(RolePermission(id="rp", role_id=role.id, permission_id=permission.id))
    db_session.add(RoleBinding(role_id=role.id, scope_id=dept.id, user_id=manager.id, include_descendants=False))
    db_session.add_all([ScopeMembership(user_id=conv.user_id, scope_id=s.id) for s in (dept, sibling)])
    await db_session.flush()
    task = Task(id="snapshot", conversation_id=cid, title="snapshot", created_at=datetime(2026, 9, 14, tzinfo=timezone.utc), token_usage={"input_tokens": 0, "output_tokens": 0}, usage_cost=Decimal(0), usage_currency="USD")
    db_session.add(task)
    await db_session.flush()
    await snapshot_usage_scopes(db_session, task, conv.user_id)
    await db_session.flush()
    # Move memberships after consumption: historical attribution remains unchanged.
    await db_session.execute(delete(ScopeMembership).where(ScopeMembership.user_id == conv.user_id, ScopeMembership.scope_id == dept.id))
    await db_session.commit()
    headers = {"Authorization": f"Bearer {create_access_token(manager.id, manager.token_version)}"}
    query = "/api/usage?since=2026-09-14&until=2026-09-14"
    body = (await http.get(query + f"&scope_id={dept.id}", headers=headers)).json()
    assert body["summary"]["task_count"] == 1
    assert body["summary"]["recorded_tasks"] == 1
    assert body["summary"]["unpriced_tasks"] == 0
    assert Decimal(body["summary"]["costs"][0]["amount"]) == 0
    assert (await http.get(query + f"&scope_id={sibling.id}", headers=headers)).status_code == 403
    assert (await http.get(query + f"&scope_id={root.id}", headers=headers)).status_code == 403
    assert (await http.get(query, headers=headers)).json()["summary"]["task_count"] == 0
    assert (await http.get(query + f"&scope_id={root.id}", headers=auth_headers)).json()["summary"]["task_count"] == 1
    assert (await http.get("/api/usage/scopes", headers=headers)).json() == [{"id": dept.id, "name": "Dept", "kind": "department"}]
    options = await http.get(f"/api/usage/options?since=2026-09-14&until=2026-09-14&scope_id={dept.id}", headers=headers)
    assert options.json()["users"] == [{"id": conv.user_id, "name": "Tester"}]
    assert (await http.get(f"/api/usage/options?scope_id={sibling.id}", headers=headers)).status_code == 403
    page = (await http.get(query + f"&scope_id={dept.id}&group_by=agent&limit=1&offset=1", headers=headers)).json()
    assert page["total_groups"] == 1 and page["groups"] == []
    assert page["summary"]["task_count"] == 1
