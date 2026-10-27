"""Personal Schedule management; triggering remains a trusted background use case."""
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from workmate_product.persistence.models import User
from workmate_product.schedules.rules import (
    ScheduleInput, SchedulePreview, ScheduleUpdate, ScheduleVersion, preview,
)
from ..auth import get_current_user
from ..db import get_db
from ..services.schedules import ScheduleService

router = APIRouter(prefix='/schedules', tags=['schedules'])


def get_schedule_service(request: Request) -> ScheduleService:
    service = getattr(request.app.state, 'schedule_service', None)
    if service is not None:
        return service
    runtime = request.app.state.product_runtime
    admission = getattr(runtime, 'admission', runtime)
    return ScheduleService(admission.sessions, admission)


@router.post('/preview')
async def schedule_preview(body: SchedulePreview, user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    now = await db.scalar(select(func.clock_timestamp()))
    return {'times': [value.isoformat() for value in preview(body, now)]}


@router.get('')
async def list_schedules(limit: int = Query(default=20, ge=1, le=100),
                         offset: int = Query(default=0, ge=0),
                         user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                         service: ScheduleService = Depends(get_schedule_service)):
    return await service.list(db, user.id, limit=limit, offset=offset)


@router.post('', status_code=201)
async def create_schedule(body: ScheduleInput, user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db),
                          service: ScheduleService = Depends(get_schedule_service)):
    schedule = await service.create(db, user.id, body)
    return await service.serialize(db, schedule)


@router.get('/notification-targets')
async def notification_targets(user: User = Depends(get_current_user),
                               db: AsyncSession = Depends(get_db),
                               service: ScheduleService = Depends(get_schedule_service)):
    return await service.notification_targets(db,user.id)


class NotificationRetry(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirm_duplicates: bool = Field(default=False, strict=True)


@router.post('/{schedule_id}/occurrences/{occurrence_id}/notification/retry')
async def retry_notification(schedule_id: str, occurrence_id: str, body: NotificationRetry,
                             user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                             service: ScheduleService = Depends(get_schedule_service)):
    return await service.retry_notification(db,user.id,schedule_id,occurrence_id,
        confirm_duplicates=body.confirm_duplicates)


@router.get('/{schedule_id}')
async def get_schedule(schedule_id: str, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db),
                       service: ScheduleService = Depends(get_schedule_service)):
    return await service.serialize(db, await service.get(db, user.id, schedule_id))


@router.patch('/{schedule_id}')
async def update_schedule(schedule_id: str, body: ScheduleUpdate,
                          user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                          service: ScheduleService = Depends(get_schedule_service)):
    return await service.serialize(db, await service.update(db, user.id, schedule_id, body))


@router.post('/{schedule_id}/pause')
async def pause_schedule(schedule_id: str, body: ScheduleVersion,
                         user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                         service: ScheduleService = Depends(get_schedule_service)):
    return await service.serialize(db, await service.set_active(db, user.id, schedule_id, body.version, False))


@router.post('/{schedule_id}/resume')
async def resume_schedule(schedule_id: str, body: ScheduleVersion,
                          user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                          service: ScheduleService = Depends(get_schedule_service)):
    return await service.serialize(db, await service.set_active(db, user.id, schedule_id, body.version, True))


@router.delete('/{schedule_id}', status_code=204)
async def delete_schedule(schedule_id: str, version: int = Query(ge=1),
                          user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                          service: ScheduleService = Depends(get_schedule_service)):
    await service.delete(db, user.id, schedule_id, version)
    return Response(status_code=204)


@router.get('/{schedule_id}/occurrences')
async def occurrence_history(schedule_id: str, limit: int = Query(default=20, ge=1, le=100),
                             offset: int = Query(default=0, ge=0),
                             user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                             service: ScheduleService = Depends(get_schedule_service)):
    return await service.occurrences(db, user.id, schedule_id, limit=limit, offset=offset)
