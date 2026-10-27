"""Exercise real response serialization, not a mocked frontend payload."""
import uuid

import pytest

from workmate_product.persistence.models import CapabilityInstallation
from app.services.bootstrap import get_root_scope


@pytest.mark.asyncio
@pytest.mark.parametrize('hub_id', ['workmate-builtin', 'uploaded', 'skillhub-cn'])
async def test_skill_list_and_update_preserve_source_metadata(
    client_and_app, auth_headers, db_session, hub_id,
):
    http, _ = client_and_app
    root = await get_root_scope(db_session)
    metadata = {
        'hub_id': hub_id,
        'card_id': 'source-contract',
        'description': 'Source metadata must reach the UI',
        'tags': ['office'],
        'central_library_origin': {'scope_id': 'old-dept', 'scope_name': 'Source department'},
    }
    skill_id = str(uuid.uuid4())
    db_session.add(CapabilityInstallation(
        id=skill_id, capability_type='skill', scope_id=root.id,
        name='source-contract', display_name='Source Contract', enabled=True,

        metadata_=metadata,
    ))
    await db_session.commit()
    response = await http.get('/api/skills', headers=auth_headers)
    assert response.status_code == 200, response.text
    skill = next(item for item in response.json()['items'] if item['id'] == skill_id)
    assert skill['metadata'] == metadata
    assert 'metadata_' not in skill
    updated = await http.patch(f'/api/skills/{skill_id}', headers=auth_headers, json={'enabled': False})
    assert updated.status_code == 200, updated.text
    assert updated.json()['metadata'] == metadata
