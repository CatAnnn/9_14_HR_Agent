from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ResourceChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2_000)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("消息内容不能为空")
        return clean


class ResourceChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2_000)
    history: list[ResourceChatTurn] = Field(default_factory=list, max_length=8)

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("消息内容不能为空")
        return clean


class ResourceChatSource(BaseModel):
    id: str
    title: str


class ResourceChatResponse(BaseModel):
    answer: str
    sources: list[ResourceChatSource] = Field(default_factory=list)
