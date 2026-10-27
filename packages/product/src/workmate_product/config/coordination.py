"""Shared validation for an optional, deployment-owned Redis connection."""
import re
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator, StrictStr


def _connection(value: str) -> str:
    if value == '':
        return value
    try:
        parsed = urlsplit(value)
        valid = (not any(character.isspace() for character in value)
                 and parsed.scheme in {'redis', 'rediss'}
                 and bool(parsed.hostname) and not parsed.fragment
                 and (parsed.port is None or 1 <= parsed.port <= 65535)
                 and re.fullmatch(r'(?:/[0-9]*)?', parsed.path) is not None)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Coordination connection must be empty or a valid redis/rediss URL')
    return value


CoordinationRedisURL = Annotated[StrictStr, AfterValidator(_connection)]
