"""Worker deployment identity and connections, supplied only by environment."""
import os
from pathlib import Path
from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator
from sqlalchemy.engine import make_url
from workmate_runtime.config import RuntimeSettings,load_runtime_settings


class WorkerRoleSettings(BaseModel):
    model_config=ConfigDict(extra='forbid')
    runtime:RuntimeSettings
    database_url:str=Field(repr=False,min_length=1)
    storage_node_id:str=Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
    workspace_dir:Path
    artifact_dir:Path

    @field_validator('database_url')
    @classmethod
    def postgres(cls,value):
        if make_url(value).drivername!='postgresql+asyncpg':
            raise ValueError('Worker Product database must use postgresql+asyncpg')
        return value

    @model_validator(mode='after')
    def storage_roots(self):
        workspace=self.workspace_dir.resolve()
        artifacts=self.artifact_dir.resolve()
        if workspace==artifacts or workspace.is_relative_to(artifacts) or artifacts.is_relative_to(workspace):
            raise ValueError('Worker Workspace and Artifact roots must be separate')
        return self


def load_worker_role_settings():
    runtime=load_runtime_settings()
    return WorkerRoleSettings(runtime=runtime,database_url=os.getenv('DATABASE_URL',''),
        storage_node_id=os.getenv('WORKMATE_WORKER_STORAGE_NODE_ID',''),
        workspace_dir=runtime.workspace_basedir,
        artifact_dir=os.getenv('ARTIFACT_STORAGE_DIR','./artifact-storage'))
