"""Explicit SSH targets for isolated reliability fixtures."""
import os
import re


def remote_target(value=None):
    target = value if value is not None else os.environ.get('WORKMATE_TEST_SSH_TARGET')
    if not target or not re.fullmatch(r'(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9_.-]*', target):
        raise ValueError('Set WORKMATE_TEST_SSH_TARGET or provide an explicit user@host target')
    return target
