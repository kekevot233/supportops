"""应用身份、空间与成员数据；请求字段不允许夹带操作者或权限。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

ResourceId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
DisplayName = Annotated[str, Field(min_length=1, max_length=200)]
Role = Literal["reader", "editor", "admin"]


class AppModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class Identity(AppModel):
    subject: ResourceId
    auth_mode: Literal["local_demo", "local_session", "external"]


class WorkspaceCreate(AppModel):
    name: DisplayName


class MembershipChange(AppModel):
    member_id: ResourceId
    role: Role


class WorkspaceView(AppModel):
    id: ResourceId
    name: DisplayName
    role: Role
    created_at: str


class MemberView(AppModel):
    user_id: ResourceId
    display_name: DisplayName
    role: Role


class AuditView(AppModel):
    id: ResourceId
    workspace_id: ResourceId
    actor_id: ResourceId
    action: str
    target_id: ResourceId
    created_at: str


__all__ = ["Identity", "WorkspaceCreate", "MembershipChange", "WorkspaceView", "MemberView", "AuditView"]
