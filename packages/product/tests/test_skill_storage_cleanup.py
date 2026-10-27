"""Object-only schema upgrades retain metadata and immutable publication facts."""

import pytest
import sqlalchemy as sa

from workmate_product.persistence.models import SkillArtifactRecord

def test_current_model_requires_object_locator():
    columns = SkillArtifactRecord.__table__.columns
    assert 'archive' not in columns
    assert not columns.storage_key.nullable
    assert not columns.size_bytes.nullable


def test_objects_require_locator_and_immutable_metadata():
    engine = sa.create_engine('sqlite:///:memory:')
    digest = 'a' * 64
    key = f'sha256/aa/{digest}.zip'
    with engine.begin() as db:
        from workmate_product.persistence.models import Base
        Base.metadata.create_all(db)
        db.execute(sa.text('''INSERT INTO skill_artifacts
            (digest,format_version,storage_key,size_bytes,manifest,name,description)
            VALUES (:d,1,:k,3,'[]','sample','description')'''),
            {'d': digest, 'k': key})
        assert 'archive' not in {c['name'] for c in sa.inspect(db).get_columns('skill_artifacts')}
        assert db.execute(sa.text('SELECT storage_key,size_bytes,name FROM skill_artifacts')).one() == (key,3,'sample')
        for sql in ("UPDATE skill_artifacts SET name='changed'", 'DELETE FROM skill_artifacts'):
            with pytest.raises(sa.exc.IntegrityError, match='immutable'):
                db.exec_driver_sql(sql)
        with pytest.raises(sa.exc.IntegrityError):
            db.execute(sa.text('''INSERT INTO skill_artifacts
                (digest,format_version,manifest,name,description) VALUES (:d,1,'[]','x','x')'''),
                {'d': 'b' * 64})
    engine.dispose()


def test_product_identity_has_no_retired_runtime_placeholders():
    from workmate_product.persistence.models import User, CapabilityInstallation, KnowledgeBase
    assert 'runtime_credential_id' not in User.__table__.columns
    assert 'runtime_owner_id' not in CapabilityInstallation.__table__.columns
    assert 'runtime_resource_id' not in CapabilityInstallation.__table__.columns
    assert 'runtime_owner_id' in KnowledgeBase.__table__.columns
