# -*- coding: utf-8 -*-
"""Audit records survive deletion of their associated scope."""


from sqlalchemy import create_engine, event, text


def test_audit_scope_is_null_after_scope_delete():
    engine = create_engine("sqlite:///:memory:")
    event.listen(
        engine, "connect",
        lambda connection, _record: connection.execute("PRAGMA foreign_keys=ON"),
    )
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE scopes (id VARCHAR(36) PRIMARY KEY)"
        ))
        conn.execute(text(
            "CREATE TABLE users (id VARCHAR(36) PRIMARY KEY)"
        ))
        from workmate_product.persistence.models import SecurityAuditEvent
        SecurityAuditEvent.__table__.create(conn)
        conn.execute(text("INSERT INTO scopes VALUES ('scope-1')"))
        conn.execute(text(
            "INSERT INTO security_audit_events "
            "(id, event_type, scope_id, metadata) VALUES "
            "('event-1', 'scope.delete', 'scope-1', '{}')"
        ))
        conn.execute(text("DELETE FROM scopes WHERE id='scope-1'"))
        assert conn.scalar(text(
            "SELECT scope_id FROM security_audit_events WHERE id='event-1'"
        )) is None
