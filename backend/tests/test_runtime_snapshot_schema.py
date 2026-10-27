"""Current model and deployment contracts."""
from sqlalchemy.orm import configure_mappers


def test_snapshot_models_registered_and_mappers_resolve():
    from sqlalchemy.orm import configure_mappers
    from workmate_product.persistence.models import Base, RuntimeSnapshot, RuntimeTaskExecution
    configure_mappers()
    assert Base.metadata.tables['runtime_snapshots'] is RuntimeSnapshot.__table__
    assert Base.metadata.tables['runtime_task_executions'] is RuntimeTaskExecution.__table__
