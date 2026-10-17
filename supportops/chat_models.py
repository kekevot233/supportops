"""问答请求与严格抽取式草稿，禁止客户端指定会话所有者和反馈身份。"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import AppModel, DisplayName


class ConversationCreate(AppModel):
    title: DisplayName = "新会话"


class QuestionRequest(AppModel):
    question: Annotated[str, Field(min_length=1, max_length=2000)]
    idempotency_key: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]


class Passage(AppModel):
    label: Annotated[str, Field(pattern=r"^D[1-9][0-9]?$" )]
    quote: Annotated[str, Field(min_length=1, max_length=1000)]


class AnswerDraft(AppModel):
    status: Literal["answered", "insufficient_evidence"]
    passages: list[Passage] = Field(max_length=5)

    @model_validator(mode="after")
    def validate_status(self):
        if (self.status == "answered") != bool(self.passages):
            raise ValueError("status and evidence do not match")
        if len({passage.label for passage in self.passages}) != len(self.passages):
            raise ValueError("duplicate evidence labels")
        return self


class FeedbackRequest(AppModel):
    rating: Literal["useful", "not_useful"]
    reason: Literal["irrelevant", "incorrect", "outdated", "other"] | None = None
    comment: Annotated[str, Field(max_length=500)] = ""
