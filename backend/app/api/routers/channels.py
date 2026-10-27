"""Authenticated enterprise connection management and personal identity linking."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from workmate_product.persistence.models import User
from ...auth import get_current_user
from ...db import get_db
from ...channels.management import ChannelManagement
from ...channels.rules import ChannelCreate, ChannelUpdate

class BoundedChannelRoute(APIRoute):
    """Limit streamed JSON before FastAPI reads or validates credential-bearing bodies."""
    def get_route_handler(self):
        original = super().get_route_handler()

        async def bounded(request: Request):
            content = bytearray()
            async for chunk in request.stream():
                if len(content) + len(chunk) > 32768:
                    raise HTTPException(413, '渠道请求过大')
                content.extend(chunk)
            # Cache the bounded body for FastAPI's subsequent Request.body()/json().
            request._body = bytes(content)
            return await original(request)
        return bounded


router = APIRouter(prefix='/channels', tags=['channels'], route_class=BoundedChannelRoute)


def get_channel_service(request: Request) -> ChannelManagement:
    service = getattr(request.app.state, 'channel_service', None)
    if service is None:
        raise HTTPException(503, '消息渠道功能未启用')
    return service


class QRStatus(BaseModel):
    model_config = ConfigDict(extra='forbid')
    qrcode: str = Field(min_length=1, max_length=4096)


@router.get('')
async def list_channels(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                        service: ChannelManagement = Depends(get_channel_service)):
    return await service.list(db,user.id)


@router.post('', status_code=201)
async def create_channel(body: ChannelCreate, user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db),
                         service: ChannelManagement = Depends(get_channel_service)):
    return await service.create(db,user.id,body)


@router.get('/bindings')
async def bindings(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                   service: ChannelManagement = Depends(get_channel_service)):
    return await service.bindings(db,user.id)


@router.delete('/bindings/{identity_id}', status_code=204)
async def unbind(identity_id: str, user: User = Depends(get_current_user),
                 db: AsyncSession = Depends(get_db), service: ChannelManagement = Depends(get_channel_service)):
    await service.unbind(db,user.id,identity_id)
    return Response(status_code=204)


@router.post('/deliveries/{delivery_id}/retry')
async def retry(delivery_id: str, user: User = Depends(get_current_user),
                db: AsyncSession = Depends(get_db), service: ChannelManagement = Depends(get_channel_service)):
    return await service.retry(db,user.id,delivery_id)


@router.patch('/{channel_id}')
async def update_channel(channel_id: str, body: ChannelUpdate, user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db), service: ChannelManagement = Depends(get_channel_service)):
    return await service.update(db,user.id,channel_id,body)


@router.delete('/{channel_id}', status_code=204)
async def delete_channel(channel_id: str, version: int = Query(ge=1),
                         user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                         service: ChannelManagement = Depends(get_channel_service)):
    await service.delete(db,user.id,channel_id,version)
    return Response(status_code=204)


@router.post('/{channel_id}/binding-code')
async def binding_code(channel_id: str, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db), service: ChannelManagement = Depends(get_channel_service)):
    return await service.binding_code(db,user.id,channel_id)


@router.get('/{channel_id}/deliveries')
async def deliveries(channel_id: str, limit: int = Query(default=20,ge=1,le=100),
                      offset: int = Query(default=0,ge=0), user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db), service: ChannelManagement = Depends(get_channel_service)):
    return await service.deliveries(db,user.id,channel_id,limit,offset)


@router.post('/{channel_id}/qr')
async def qr(channel_id: str, user: User = Depends(get_current_user),
             db: AsyncSession = Depends(get_db), service: ChannelManagement = Depends(get_channel_service)):
    return await service.qr(db,user.id,channel_id)


@router.post('/{channel_id}/qr-status')
async def qr_status(channel_id: str, body: QRStatus, user: User = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db), service: ChannelManagement = Depends(get_channel_service)):
    return await service.qr_status(db,user.id,channel_id,body.qrcode)
