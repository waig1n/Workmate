"""Durable user/Agent storage affinity shared by local memory and multi-node admission."""
from datetime import datetime
from sqlalchemy import DateTime,ForeignKey,String,func
from sqlalchemy.orm import Mapped,mapped_column
from .models import Base


class RuntimeMemoryNamespace(Base):
    __tablename__='runtime_memory_namespaces'
    user_id:Mapped[str]=mapped_column(ForeignKey('users.id'),primary_key=True)
    agent_id:Mapped[str]=mapped_column(ForeignKey('agents.id'),primary_key=True)
    storage_node_id:Mapped[str]=mapped_column(String(128))
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=func.now())
