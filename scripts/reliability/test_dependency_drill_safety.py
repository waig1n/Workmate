"""Fault injection refuses non-owned containers before any mutation."""
import pytest
from scripts.reliability.dependency_drill import validate_owner


def test_owned_dependency_allowed():
    owner='fault-dependency-0123456789ab'
    validate_owner(owner,owner+'-redis',{'workmate.validation.owner':owner})


@pytest.mark.parametrize('owner,name,labels',[
    ('workmate','workmate-redis',{'workmate.validation.owner':'workmate'}),
    ('fault-dependency-0123456789ab','workmate-redis-1',{'workmate.validation.owner':'fault-dependency-0123456789ab'}),
    ('fault-dependency-0123456789ab','fault-dependency-0123456789ab-postgres',{}),
    ('fault-dependency-0123456789ab','fault-dependency-0123456789ab-postgres',{'workmate.validation.owner':'other'}),
])
def test_unowned_target_rejected(owner,name,labels):
    with pytest.raises(ValueError): validate_owner(owner,name,labels)
