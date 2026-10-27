# -*- coding: utf-8 -*-
"""Initialize or upgrade the Workmate schema before starting the product API."""

import os
from pathlib import Path
import subprocess
import sys


def _config_path() -> str:
    return os.getenv("WORKMATE_ALEMBIC_CONFIG", "/app/alembic.ini")


def _upgrade(config: str) -> None:
    # Alembic validates revision ancestry before running migrations.
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", config,
         "upgrade", "head"],
        check=True,
    )


def main() -> None:
    config = _config_path()
    if not Path(config).is_file():
        raise RuntimeError(f"Alembic config does not exist: {config}")
    _upgrade(config)
    os.execvp("uvicorn", [
        "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080",
    ])


if __name__ == "__main__":
    main()
