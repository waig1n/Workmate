import asyncio
import hashlib
import pytest
from scripts.reliability.load_support.skill_fixture import FixtureStore, archive


def test_store_checks_digest_and_size(tmp_path):
    async def scenario():
        store=FixtureStore(tmp_path);raw=archive();digest=hashlib.sha256(raw).hexdigest()
        await store.put_verified(digest,raw)
        assert await store.get_verified(digest,len(raw))==raw
        with pytest.raises(AssertionError):await store.get_verified(digest,len(raw)+1)
        with pytest.raises(AssertionError):await store.put_verified(digest,b'wrong')
        with pytest.raises(ValueError):await store.get_verified('../escape',1)
    asyncio.run(scenario())
