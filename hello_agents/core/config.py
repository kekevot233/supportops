"""HelloAgents 的非敏感运行配置。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .exceptions import ConfigurationError


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Config(BaseModel):
    """集中保存模型调用和 Agent 运行时的非敏感配置。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    default_model: Annotated[str, Field(min_length=1)] = "deepseek-chat"
    default_provider: Annotated[str, Field(min_length=1)] = "auto"
    temperature: Annotated[float, Field(ge=0.0, le=2.0)] = 0.7
    max_tokens: Annotated[int | None, Field(gt=0)] = None
    request_timeout_seconds: Annotated[int, Field(gt=0)] = 60
    debug: bool = False
    log_level: LogLevel = "INFO"
    max_history_length: Annotated[int, Field(gt=0)] = 100

    @field_validator("default_model", "default_provider", mode="before")
    @classmethod
    def reject_blank_text(cls, value: object) -> object:
        """去除首尾空格，并拒绝空的模型名或供应商名。"""
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("不能为空。")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        """允许环境变量使用小写日志级别，内部统一保存大写。"""
        return value.strip().upper() if isinstance(value, str) else value

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Config":
        """从环境变量映射创建配置，不加载或读取 .env 文件。"""
        source = os.environ if environ is None else environ
        environment_to_field = {
            "LLM_MODEL_ID": "default_model",
            "LLM_PROVIDER": "default_provider",
            "LLM_TEMPERATURE": "temperature",
            "LLM_MAX_TOKENS": "max_tokens",
            "LLM_TIMEOUT": "request_timeout_seconds",
            "DEBUG": "debug",
            "LOG_LEVEL": "log_level",
            "MAX_HISTORY_LENGTH": "max_history_length",
        }
        values = {
            field: source[environment_name]
            for environment_name, field in environment_to_field.items()
            if environment_name in source
        }

        try:
            return cls(**values)
        except ValidationError as error:
            raise ConfigurationError("环境变量中的 HelloAgents 配置无效。") from error
