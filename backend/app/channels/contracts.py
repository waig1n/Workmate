"""Private transport contracts; reply context must never enter public DTOs."""
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class InboundMessage:
    message_id: str
    sender_id: str
    chat_id: str
    chat_type: Literal['private', 'group']
    text: str
    mentioned: bool = False
    reply_context: dict[str, Any] = field(default_factory=dict, repr=False)
    unsupported: bool = False

    def __post_init__(self):
        if not all(isinstance(value, str) and value for value in (self.message_id, self.sender_id, self.chat_id)):
            raise ValueError('Channel message identity missing')
        if self.chat_type not in ('private', 'group'):
            raise ValueError('Invalid chat type')

    def target(self) -> dict:
        return {'chat_id': self.chat_id, 'sender_id': self.sender_id,
                'chat_type': self.chat_type, 'reply_context': dict(self.reply_context)}
