"""工单只接受业务字段，身份与模型调用模式均由服务端决定。"""
from typing import Annotated, Literal

from pydantic import Field, field_validator

from .models import AppModel, DisplayName, ResourceId

IdempotencyKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]


class TicketCreate(AppModel):
    title: DisplayName
    description: Annotated[str, Field(min_length=1, max_length=1600)]
    category: Literal["general", "installation", "documents", "memory", "tools"] = "general"
    priority: Literal["low", "normal", "high"] = "normal"
    idempotency_key: IdempotencyKey


class TicketOperation(AppModel):
    expected_revision: Annotated[int, Field(ge=0, le=1000)]
    idempotency_key: IdempotencyKey


class TicketAccept(TicketOperation):
    suggestion_id: ResourceId
    confirm: Literal[True]
    note: Annotated[str, Field(max_length=2000)] = ""

    @field_validator("confirm", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit boolean confirmation required")
        return value


class TicketNote(TicketOperation):
    content: Annotated[str, Field(min_length=1, max_length=2000)]


class TicketResolve(TicketNote):
    confirm: Literal[True]

    @field_validator("confirm", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit boolean confirmation required")
        return value
