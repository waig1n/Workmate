"""Artifact and upload storage policy shared by Product and Worker."""
from typing import Literal
from pydantic import Field, model_validator
from .skill_storage import SkillStoragePolicy


class ArtifactStoragePolicy(SkillStoragePolicy):
    backend: Literal['local', 's3'] = 'local'
    workspace_checkpoint_enabled: bool = False
    workspace_checkpoint_max_files: int = Field(default=1000, strict=True, ge=1, le=100000)
    workspace_checkpoint_max_file_bytes: int = Field(default=16777216, strict=True, ge=1, le=1073741824)
    workspace_checkpoint_max_bytes: int = Field(default=134217728, strict=True, ge=1, le=10737418240)
    tool_content_enabled: bool = False
    tool_content_inline_bytes: int = Field(default=65536, ge=1024, le=131072)
    tool_content_preview_chars: int = Field(default=2048, ge=128, le=8192)

    @model_validator(mode="after")
    def checkpoints_require_s3(self):
        if self.workspace_checkpoint_enabled and self.backend != "s3":
            raise ValueError("Workspace checkpoints require S3 artifact storage")
        if self.workspace_checkpoint_max_file_bytes > self.workspace_checkpoint_max_bytes:
            raise ValueError("Checkpoint file limit exceeds total limit")
        return self


def artifact_storage_overrides(environment):
    result = {}
    for field in ('backend', 'region', 'timeout_seconds', 'tool_content_enabled',
                  'tool_content_inline_bytes', 'tool_content_preview_chars',
                  'workspace_checkpoint_enabled', 'workspace_checkpoint_max_files',
                  'workspace_checkpoint_max_file_bytes', 'workspace_checkpoint_max_bytes'):
        name = 'ARTIFACT_STORAGE_' + field.upper()
        value = environment.get(name, environment.get(name.lower()))
        if value is not None and value != '':
            result[field] = (float(value) if field == 'timeout_seconds' else
                             int(value) if field in {'tool_content_inline_bytes', 'tool_content_preview_chars',
                                 'workspace_checkpoint_max_files', 'workspace_checkpoint_max_file_bytes',
                                 'workspace_checkpoint_max_bytes'} else value)
    return result
