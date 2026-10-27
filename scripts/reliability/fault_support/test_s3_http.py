"""Catch fixture/schema drift before allocating fault-test infrastructure."""
def test_metadata_observation_tables_match_product_schema():
    from workmate_product.persistence.models import Base
    from scripts.reliability.fault_support.s3_http import METADATA_TABLES
    assert set(METADATA_TABLES) <= set(Base.metadata.tables)
    assert {'tasks', 'runtime_commands', 'artifacts', 'task_messages'} <= set(METADATA_TABLES)


def test_child_exit_requires_normal_exit_or_our_explicit_sigterm():
    import signal
    from scripts.reliability.fault_support.s3_http import expected_role_exit
    assert expected_role_exit(0, False)
    assert expected_role_exit(-signal.SIGTERM, True)
    assert not expected_role_exit(-signal.SIGTERM, False)
    assert not expected_role_exit(-signal.SIGKILL, True)
    assert not expected_role_exit(1, True)
