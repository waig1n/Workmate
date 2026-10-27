"""HTTP admission and response-owned cleanup for the realtime SSE handle."""
import anyio
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from redis.exceptions import RedisError

from workmate_product.projections.store import ProjectionConflict
from workmate_product.persistence.repositories.event_logs import LogUnavailable
from workmate_product.realtime.reader import GenerationMismatch, ReaderCapacityExceeded, ReaderClosed
from workmate_product.realtime.redis_log import LogLost, ReplayGap
from workmate_product.realtime.reset_builder import ResetViewChanged

from .conversation_business_state import (
    ConversationBusinessState, ConversationUnavailable, ResetCapacityExceeded, ResetStateConflict,
)
from .realtime_product_stream import StreamCapacityExceeded


class _HandleResponse(StreamingResponse):
    def __init__(self, handle, body, **kwargs):
        super().__init__(body, **kwargs)
        self.handle = handle

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # A client may disconnect before the generator is first consumed,
            # or while response.start is being sent. Response owns the handle.
            with anyio.CancelScope(shield=True):
                try:
                    await self.body_iterator.aclose()
                finally:
                    await self.handle.aclose()


async def realtime_response(request, stream, conversation_id, *, actor_id,
                            token_version, last_event_id):
    authority = ConversationBusinessState(request.app.state.product_sessions,
        actor_id, token_version=token_version, policy=stream.policy)
    try:
        handle = await stream.open(conversation_id, authority=authority, last_event_id=last_event_id)
    except ConversationUnavailable as error:
        raise HTTPException(status_code=404, detail='Conversation not found') from error
    except ResetCapacityExceeded as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    except (ResetStateConflict, ProjectionConflict) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (StreamCapacityExceeded, ReaderCapacityExceeded, ResetViewChanged, LogUnavailable,
            LogLost, ReplayGap, GenerationMismatch, ReaderClosed, RedisError, TimeoutError) as error:
        raise HTTPException(status_code=503, detail='Realtime stream is temporarily unavailable',
                            headers={'Retry-After': '2'}) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    async def frames():
        iterator = handle.frames()
        try:
            async for frame in iterator:
                if await request.is_disconnected():
                    return
                yield frame
        except ConversationUnavailable:
            return
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await iterator.aclose()
                finally:
                    await handle.aclose()

    return _HandleResponse(handle, frames(), media_type='text/event-stream', headers={
        'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no',
    })
