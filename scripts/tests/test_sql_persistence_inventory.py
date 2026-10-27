"""Static inventory must not confuse SQLAlchemy expressions with raw SQL."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "sql_persistence_inventory.py"


def load():
    spec = importlib.util.spec_from_file_location("sql_inventory", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_distinguishes_text_driver_and_orm_calls():
    rows = load().inventory_source('''
from sqlalchemy import text as sql
import sqlalchemy as sa
async def read(db):
    await db.execute(sql("SELECT id FROM tasks"))
    await db.fetchrow("UPDATE tasks SET status=$1 RETURNING id", status)
    await db.execute(sa.select(Task))
    await db.execute(sa.text("DELETE FROM tasks WHERE id=:id"))
''', "backend/app/example.py")
    assert [(r["function"], r["style"], r["operation"]) for r in rows] == [
        ("read", "text SQL", "read"),
        ("read", "driver SQL", "update"),
        ("read", "text SQL", "delete"),
    ]
    assert all(r["transaction_owner"] == "manual review required" for r in rows)


def test_constants_dynamic_sql_and_nested_functions():
    rows = load().inventory_source('''
from sqlalchemy import text
QUERY = "SELECT * FROM tasks FOR UPDATE"
class Store:
    async def lock(self, db, table):
        await db.fetch(QUERY)
        await db.execute(f"DELETE FROM {table}")
        await db.execute(text(build_query()))
        def nested():
            return text("SELECT pg_advisory_xact_lock(1)")
''', "runtime/workmate_runtime/transport/example.py")
    assert len(rows) == 4
    assert rows[0]["lock_semantics"] == ["FOR UPDATE"]
    assert rows[1]["dynamic"] is True
    assert rows[2]["operation"] == "unknown"
    assert rows[3]["function"] == "Store.lock.nested"
    assert all(r["recommendation"] == "Protocol review" for r in rows)


def test_ignores_non_sql_strings_and_marks_unresolved_driver_calls():
    rows = load().inventory_source('''
async def run(db, stmt):
    await db.execute(stmt)
    await db.fetchrow(stmt)
    await db.execute("hello")
''', "x.py")
    assert len(rows) == 1
    assert rows[0]["style"] == "unresolved driver call"


def test_report_excludes_tests_and_has_reproduction_metadata(tmp_path):
    folder = tmp_path / "backend/app"
    folder.mkdir(parents=True)
    (folder / "a.py").write_text('from sqlalchemy import text\nx=text("SELECT 1")')
    (folder / "test_a.py").write_text('from sqlalchemy import text\nx=text("SELECT 2")')
    report = load().build_report(tmp_path, "abc")
    assert report["baseline_commit"] == "abc"
    assert report["total_calls"] == 1
    assert "python scripts/sql_persistence_inventory.py" in report["reproduce"]


def test_parse_failure_is_not_silently_ignored():
    with pytest.raises(SyntaxError):
        load().inventory_source("not python !", "bad.py")


def test_sql_fragments_and_atomic_candidates():
    rows = load().inventory_source('''
from sqlalchemy.sql import text
prefix = "SELECT "
x = text(prefix + "id FROM tasks")
y = text("INSERT INTO task_events VALUES (:id) RETURNING id")
z = text("UPDATE runtime_sessions SET lease_until=clock_timestamp()")
''', "example.py")
    assert [r["recommendation"] for r in rows] == ["Query boundary review", "Atomic operation review", "Protocol review"]
    assert rows[0]["dynamic"] is False
    assert rows[1]["postgresql_specific"] == ["RETURNING"]


def test_cli_writes_json_or_stdout(tmp_path, monkeypatch, capsys):
    module = load()
    monkeypatch.setattr(module.subprocess, "check_output", lambda *a, **kw: "baseline\n")
    output = tmp_path / "report.json"
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "--root", str(tmp_path), "--output", str(output)])
    module.main()
    assert '"baseline_commit": "baseline"' in output.read_text()
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "--root", str(tmp_path)])
    module.main()
    assert '"total_calls": 0' in capsys.readouterr().out


def test_helper_sql_is_visible_without_counting_nested_text_twice():
    rows = load().inventory_source('''
from sqlalchemy import text
async def lookup(db, query):
    return await db.execute(text(query))
async def run(db, statement):
    await row(db, "SELECT id FROM tasks", id=1)
    await lookup(db, "UPDATE tasks SET status=:status")
    await row(db, text("DELETE FROM tasks"))
    await row(db, statement)
''', 'packages/product/src/workmate_product/application/example.py')
    assert [r['style'] for r in rows] == [
        'text SQL', 'helper SQL', 'helper SQL', 'text SQL', 'unresolved helper call',
    ]
    assert rows[1]['operation'] == 'read'
    assert rows[-1]['dynamic'] is True


def test_existing_repository_requires_review_without_conversion_recommendation():
    rows = load().inventory_source('''
from sqlalchemy import text
read = text("SELECT id FROM scopes")
''', 'packages/product/src/workmate_product/persistence/repositories/scopes.py')
    assert rows[0]['recommendation'] == 'Boundary review'
    assert rows[0]['review_status'] == 'manual review required'


def test_report_explicitly_excludes_embedded_data_analysis_scripts(tmp_path):
    folder = tmp_path / 'packages/product/src/workmate_product/skills/builtin_skills/data-analysis/scripts'
    folder.mkdir(parents=True)
    (folder / 'analyze.py').write_text('db.execute("SELECT * FROM user_dataset")')
    report = load().build_report(tmp_path, 'abc')
    assert report['total_calls'] == 0
    assert report['excluded_paths'] == [folder.relative_to(tmp_path).as_posix()]
    assert 'user data' in report['exclusion_reason']


def test_helper_alias_keywords_and_dynamic_builder_are_reported():
    rows = load().inventory_source('''
from sqlalchemy import text
from helpers import row as one
async def run(db):
    await one(db, sql="SELECT id FROM tasks")
    await one(db, build_query())
    await db.fetchrow(text("SELECT id FROM tasks"))
''', 'example.py')
    assert [r['style'] for r in rows] == ['helper SQL', 'unresolved helper call', 'text SQL']
