"""Production deployment exposes one Product API and one durable Worker role."""

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "path",
    (
        "backend/Dockerfile",
        "runtime/Dockerfile",
        "docker-compose.yml",
        "backend/scripts/start_product_api.py",
        "backend/pyproject.toml",
        "runtime/pyproject.toml",
    ),
)
def test_deployment_cannot_select_retired_execution_servers(path):
    source = (ROOT / path).read_text()
    assert "app.native_main:app" not in source
    assert "workmate_runtime.main:app" not in source
    assert not any(
        line.strip().lower().startswith("from ")
        and line.strip().lower().endswith(" as legacy")
        for line in source.splitlines()
    ), f"Retired image build target remains in {path}"


def test_product_entrypoint_still_registers_business_routes():
    from app.main import build_app

    app = build_app()
    paths = set(app.openapi()["paths"])
    # Building the ASGI application must retain user-facing product operations;
    # lifespan startup and its external services are tested independently.
    for resource in ("agents", "conversations", "tasks", "skills", "knowledge-bases"):
        assert any(f"/{resource}" in path for path in paths), resource
    assert any(getattr(route, "path", None) == "/health" for route in app.routes)


def test_worker_console_script_points_to_durable_role():
    import tomllib

    project = tomllib.loads((ROOT / "runtime/pyproject.toml").read_text())
    entrypoint = project["project"]["scripts"]["workmate-worker"]
    module, function = entrypoint.split(":")
    assert module == "workmate_runtime.roles.worker"
    tree = ast.parse((ROOT / "runtime" / (module.replace(".", "/") + ".py")).read_text())
    assert any(isinstance(node, ast.FunctionDef) and node.name == function for node in tree.body)
