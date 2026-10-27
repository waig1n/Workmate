from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from workmate_product.application.platform_types import PlatformToolError as HTTPException
from workmate_product.application.agent_mutations import check_agent_version as _check_agent_version


@pytest.mark.parametrize("expected", ["2026-09-10T10:50:16.039953Z", "2026-09-10T10:50:16.039953+00:00", "2026-09-10T18:50:16.039953+08:00"])
def test_equivalent_timestamp_formats(expected):
    _check_agent_version(datetime(2026, 9, 10, 10, 50, 16, 39953, tzinfo=timezone.utc), expected)


def test_changed_and_invalid_versions_fail():
    for expected, status in [("2026-09-10T10:50:16.039954Z", 409), ("not-a-date", 422)]:
        with pytest.raises(HTTPException) as error:
            _check_agent_version(datetime(2026, 9, 10, 10, 50, 16, 39953), expected)
        assert error.value.status_code == status
