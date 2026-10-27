"""Shared Skill object-storage policy and connection validation; no I/O."""
from ipaddress import ip_address
import re
from typing import Annotated
from urllib.parse import urlsplit
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints


class SkillStoragePolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    timeout_seconds: float = Field(default=30, gt=0, le=300, strict=True)
    region: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] = 'us-east-1'


def _endpoint(value: str) -> str:
    if value == '':
        return value  # Inert Settings construction is allowed; factories require credentials.
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or '?' in value or '#' in value or any(c.isspace() for c in value)):
            raise ValueError
        _ = parsed.port
    except ValueError:
        raise ValueError('Skill S3 endpoint requires HTTP(S) without credentials, query or fragment') from None
    return value


def _bucket(value: str) -> str:
    if (not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', value)
            or any(part in value for part in ('..', '.-', '-.'))
            or value.startswith(('xn--', 'sthree-', 'amzn-s3-demo-'))
            or value.endswith(('-s3alias', '--ol-s3', '.mrap', '--x-s3', '--table-s3'))):
        raise ValueError('Skill bucket must be a valid lowercase S3 bucket name')
    try:
        ip_address(value)
    except ValueError:
        return value
    raise ValueError('Skill bucket must not be an IP address')


SkillS3Endpoint = Annotated[str, AfterValidator(_endpoint)]
SkillS3Bucket = Annotated[str, AfterValidator(_bucket)]


def skill_storage_overrides(environment):
    """Read the same flat policy overrides from OS or Pydantic dotenv mappings."""
    values = {}
    for field in ('timeout_seconds', 'region'):
        name = 'SKILL_STORAGE_' + field.upper()
        value = environment.get(name, environment.get(name.lower()))
        if value is not None and value != '':
            values[field] = float(value) if field == 'timeout_seconds' else value
    return values
