# -*- coding: utf-8 -*-
"""Fresh-process import boundary regressions."""

import subprocess
import sys
import os
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
ROOT = BACKEND_ROOT.parent


def test_services_bootstrap_imports_without_auth_service_cycle():
    """Bootstrap must collect without relying on another test's import order."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import app.auth as auth; "
                "assert set(auth.__all__) <= set(dir(auth)); "
                "assert 'app.auth.deps' not in sys.modules; "
                "assert 'app.auth.security' not in sys.modules; "
                "from app.services.bootstrap import get_root_scope; "
                "from app.services import ModelConfigError"
            ),
        ],
        cwd=BACKEND_ROOT,
        env={**os.environ, 'PYTHONPATH': os.pathsep.join(str(ROOT / path) for path in
             ('backend', 'packages/contracts/src', 'packages/product/src'))},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
