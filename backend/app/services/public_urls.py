"""Browser-facing Workmate links shared by Product notifications and channels.

The deployment URL is validated by Settings; request Host/Origin and internal
container addresses are never used to infer it.
"""
from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True)
class WorkmateLinks:
    base_url: str

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip())

    def _url(self, path: str) -> str:
        if not self.configured:
            raise ValueError('WORKMATE_PUBLIC_URL is not configured')
        return self.base_url.strip().rstrip('/') + path

    def conversation(self, conversation_id: str) -> str:
        return self._url('/workspace/c/' + quote(conversation_id, safe=''))

    def channels(self) -> str:
        return self._url('/workspace/channels')
