# -*- coding: utf-8 -*-
"""Opt-in real Alembic path test against a disposable PostgreSQL DB."""

import os
from pathlib import Path
import subprocess
import sys

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory


@pytest.mark.integration
def test_fresh_postgres_database_reaches_guarded_head():
    database_url = os.getenv("WORKMATE_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("set WORKMATE_TEST_POSTGRES_URL to a fresh disposable database")
    backend_dir = Path(__file__).parents[1]
    environment = {**os.environ, "DATABASE_URL": database_url}
    current = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=backend_dir,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert current.returncode == 0, current.stderr
    if current.stdout.strip():
        pytest.skip("WORKMATE_TEST_POSTGRES_URL must point to a fresh empty database")

    upgraded = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend_dir,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    head = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=backend_dir,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert head.returncode == 0, head.stderr
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    expected_head = ScriptDirectory.from_config(config).get_current_head()
    assert expected_head is not None
    assert head.stdout.strip() == f"{expected_head} (head)"
