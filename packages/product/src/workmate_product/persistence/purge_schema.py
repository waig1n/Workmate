"""Narrow immutable-content deletion exception after trusted retirement."""
CONTENT_GUARDS = {
    'runtime_snapshots': ('reject_runtime_snapshot_mutation', 'runtime_snapshot_no_delete'),
    'runtime_execution_specs': ('reject_runtime_execution_spec_mutation', 'runtime_execution_spec_no_delete'),
    'runtime_external_receipts': ('reject_external_receipt_mutation', 'external_receipt_no_delete'),
    **{name: ('reject_' + name + '_mutation', name + '_no_delete') for name in (
        'runtime_delivery_baselines', 'runtime_delivery_captures', 'runtime_delivery_seals')},
}

RETIREMENT_GUARD = """CREATE OR REPLACE FUNCTION protect_conversation_retirement() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'retirement requests are retained'; END IF;
 IF (to_jsonb(NEW) - 'completed_at' - 'completed_by' - 'purged_at' - 'purged_by') IS DISTINCT FROM
    (to_jsonb(OLD) - 'completed_at' - 'completed_by' - 'purged_at' - 'purged_by') OR
    (OLD.completed_at IS NOT NULL AND (NEW.completed_at IS DISTINCT FROM OLD.completed_at OR NEW.completed_by IS DISTINCT FROM OLD.completed_by)) OR
    (OLD.purged_at IS NOT NULL AND NEW IS DISTINCT FROM OLD) OR
    (NEW.completed_at IS NULL) <> (NEW.completed_by IS NULL) OR
    (NEW.purged_at IS NULL) <> (NEW.purged_by IS NULL) OR
    (NEW.purged_at IS NOT NULL AND NEW.completed_at IS NULL)
 THEN RAISE EXCEPTION 'retirement identity and receipts are immutable'; END IF;
 RETURN NEW;
END $$"""


def install_purge_guards(connection):
    from sqlalchemy import inspect
    tables = set(inspect(connection).get_table_names())
    dialect = connection.dialect.name
    predicate = """EXISTS (SELECT 1 FROM conversation_retirements r
        JOIN conversations c ON c.id=r.conversation_id
        JOIN runtime_sessions s ON s.id=r.session_id
        WHERE r.session_id=OLD.session_id AND r.completed_at IS NOT NULL
        AND r.purged_at IS NULL AND c.deleted_at IS NOT NULL
        AND c.active_product_task_id IS NULL AND s.active_task_id IS NULL)"""
    if dialect == 'postgresql':
        connection.exec_driver_sql(RETIREMENT_GUARD)
        for table, (function, _) in CONTENT_GUARDS.items():
            if table not in tables:
                continue
            connection.exec_driver_sql(f"""CREATE OR REPLACE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF TG_OP='DELETE' AND {predicate} THEN RETURN OLD; END IF;
              RAISE EXCEPTION 'execution content is immutable until retired';
            END $$""")
    elif dialect == 'sqlite':
        for table, (_, trigger) in CONTENT_GUARDS.items():
            if table not in tables:
                continue
            connection.exec_driver_sql(f'DROP TRIGGER IF EXISTS {trigger}')
            connection.exec_driver_sql(f"""CREATE TRIGGER {trigger} BEFORE DELETE ON {table}
                WHEN NOT {predicate}
                BEGIN SELECT RAISE(ABORT, 'execution content is immutable until retired'); END""")
