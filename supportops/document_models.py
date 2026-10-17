"""P2 请求与公开结果，不返回原件内部对象键、执行令牌或本机路径。"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import Field, field_validator

from .models import AppModel, DisplayName, ResourceId


class UploadRequest(AppModel):
    filename: Annotated[str, Field(min_length=1, max_length=200)]
    title: DisplayName
    idempotency_key: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value):
        if (value.startswith(".") or any(char in value for char in "/\\:") or any(ord(char) < 32 for char in value)
            or PurePosixPath(value).suffix.lower() not in {".txt", ".md", ".markdown"}):
            raise ValueError("only simple text document names")
        return value


class JobView(AppModel):
    id: ResourceId
    workspace_id: ResourceId
    document_id: ResourceId
    version_id: ResourceId
    state: Literal["pending", "running", "succeeded", "failed", "cancelled"]
    attempts: int
    error_code: str | None


class DocumentView(AppModel):
    id: ResourceId
    workspace_id: ResourceId
    title: DisplayName
    current_version_id: ResourceId | None
    latest_version_id: ResourceId
    latest_version_number: int
    latest_state: Literal["pending", "ready", "failed", "deleted"]


__all__ = ["UploadRequest", "JobView", "DocumentView"]
