from __future__ import annotations

from fastapi import APIRouter, Depends, Header

from backend.api.dependencies import get_resource_chat_service
from backend.core.auth_dependency import get_current_user
from backend.schemas.auth import AuthUserResponse
from backend.schemas.locale import locale_from_accept_language
from backend.schemas.resource_chat import (
    ResourceChatRequest,
    ResourceChatResponse,
    ResourceChatSource,
)
from backend.services.resource_chat_service import ResourceChatService


router = APIRouter(prefix="/resource-chat", tags=["resource-chat"])


@router.post("/message", response_model=ResourceChatResponse)
async def send_resource_chat_message(
    payload: ResourceChatRequest,
    accept_language: str | None = Header(default=None, alias="Accept-Language"),
    current_user: AuthUserResponse = Depends(get_current_user),
    service: ResourceChatService = Depends(get_resource_chat_service),
) -> ResourceChatResponse:
    answer, chunks = await service.answer(
        payload.message,
        history=[(turn.role, turn.content) for turn in payload.history],
        requester=current_user.email,
        locale=locale_from_accept_language(accept_language),
    )
    sources: list[ResourceChatSource] = []
    seen: set[str] = set()
    for chunk in chunks:
        if chunk.source_id in seen:
            continue
        seen.add(chunk.source_id)
        sources.append(ResourceChatSource(id=chunk.source_id, title=chunk.title))
    return ResourceChatResponse(answer=answer, sources=sources)
