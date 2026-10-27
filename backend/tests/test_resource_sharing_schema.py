
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def test_sharing_unique_resource_and_target_constraints():
    with create_engine('sqlite://').begin() as connection:
        from workmate_product.persistence.models import Base
        Base.metadata.create_all(connection)
        connection.execute(text("INSERT INTO resource_shares (id, agent_id, target_scope_id) VALUES ('share', 'agent', 'target')"))
        with pytest.raises(IntegrityError):
            connection.execute(text("INSERT INTO resource_shares (id, agent_id, target_scope_id) VALUES ('duplicate', 'agent', 'target')"))
        with pytest.raises(IntegrityError):
            connection.execute(text("INSERT INTO resource_shares (id, target_scope_id) VALUES ('invalid', 'target')"))
