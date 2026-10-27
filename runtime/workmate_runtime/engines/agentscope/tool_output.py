"""Normalize external tool text before Core emits events or saves context."""
from contextlib import aclosing
import re

from agentscope.message import TextBlock
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolChunk, ToolResponse


_INVALID_TEXT = re.compile('[\x00\ud800-\udfff]')


def normalize_tool_text(text: str) -> str:
    """Keep Unicode text; replace NUL and surrogate code points with visible U+FFFD.

    Python strings must contain Unicode scalars, not UTF-16 code units. Replace
    each surrogate independently so splitting text across chunks cannot change
    the result. Real emoji and literal backslash escapes remain unchanged.
    Never use this on tool arguments or identities.
    """
    if not _INVALID_TEXT.search(text):
        return text
    return _INVALID_TEXT.sub('\ufffd', text)


class ToolOutputCharacters(MiddlewareBase):
    """Outermost acting middleware: clean chunks AND the accumulated response.

    Toolkit accumulates its original chunks after yielding them. Cleaning only
    the yielded copy would leave invalid text in the final Core snapshot.
    Only display text changes; arguments, IDs, metadata and media are untouched.
    """

    async def on_acting(self, agent, input_kwargs, next_handler):
        async with aclosing(next_handler(**input_kwargs)) as stream:
            async for result in stream:
                if isinstance(result, (ToolChunk, ToolResponse)):
                    result = result.model_copy(update={'content': [
                        block.model_copy(update={'text': normalize_tool_text(block.text)})
                        if isinstance(block, TextBlock) else block
                        for block in result.content
                    ]})
                yield result
