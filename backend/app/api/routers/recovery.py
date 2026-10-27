"""Explicit new-session recovery inside the same conversation, never old-owner takeover."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, StrictBool
from sqlalchemy.ext.asyncio import AsyncSession
from ...auth import get_current_user
from ...db import get_db
from workmate_product.persistence.models import User
from ...services.recovery_forks import preview, recover

router=APIRouter(prefix='/conversations',tags=['conversations'])

class RecoveryRequest(BaseModel):
    request_id: UUID
    acknowledge_data_loss: StrictBool

@router.get('/{conversation_id}/recovery')
async def recovery_preview(conversation_id:str, request:Request,
                           user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    return await preview(db,user.id,conversation_id,request.app.state.product_runtime.admission)

@router.post('/{conversation_id}/recovery',status_code=202)
async def create_recovery(conversation_id:str,body:RecoveryRequest,request:Request,
                          user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    if body.acknowledge_data_loss is not True:
        raise HTTPException(422,'请确认未保存修改可能丢失及外部操作可能重复')
    return await recover(db,user,conversation_id,str(body.request_id),request.app.state.product_runtime.admission)
