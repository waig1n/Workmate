"""The deployment shortcut uses the shared configuration and preserves arguments."""
from pathlib import Path
import shutil
import subprocess


def test_launcher_delegates_from_repository_root(tmp_path):
    root = Path(__file__).parents[2]
    (tmp_path / 'deploy').mkdir()
    (tmp_path / 'scripts').mkdir()
    shutil.copy2(root / 'deploy/start.sh', tmp_path / 'deploy/start.sh')
    helper = tmp_path / 'scripts/workmate'
    helper.write_text('#!/bin/sh\npwd\nprintf "%s\\n" "$@"\n')
    helper.chmod(0o755)
    result = subprocess.run(['sh', str(tmp_path / 'deploy/start.sh'), '--dry-run'], cwd='/', check=True, capture_output=True, text=True)
    assert result.stdout.splitlines() == [str(tmp_path), 'up', '--dry-run']
