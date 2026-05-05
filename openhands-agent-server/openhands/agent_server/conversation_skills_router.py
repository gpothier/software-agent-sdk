"""Per-conversation skill management endpoints.

Provides clean endpoints for listing, activating, and deactivating skills
within a specific conversation. Skills can be in one of two states:
- discovered: listed in <available_skills> in the system prompt (agentskills-format)
- active: content included in <REPO_CONTEXT> every turn (trigger=None)
"""

from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from fastapi.params import Depends
from pydantic import BaseModel

from openhands.agent_server.conversation_service import ConversationService
from openhands.agent_server.dependencies import get_conversation_service


conversation_skills_router = APIRouter(tags=["Conversation Skills"])


class ConversationSkillInfo(BaseModel):
    """A skill with its current activation state in a conversation."""

    name: str
    source: str | None = None
    description: str | None = None
    always: bool = False
    triggers: list[str] = []
    state: Literal["active", "discovered"]


class ConversationSkillDetail(ConversationSkillInfo):
    """A skill with its full content (re-read from disk on request)."""

    content: str


def _to_detail(skill_info, state: str) -> ConversationSkillDetail:
    source = skill_info.source
    content = ""
    if source:
        try:
            content = Path(source).read_text(encoding="utf-8")
        except FileNotFoundError:
            content = "[skill file not found]"
    else:
        content = skill_info.content
    return ConversationSkillDetail(
        name=skill_info.name,
        source=source,
        description=skill_info.description,
        always=skill_info.always,
        triggers=skill_info.triggers,
        state=state,
        content=content,
    )


@conversation_skills_router.get(
    "/conversations/{conversation_id}/skills",
    response_model=list[ConversationSkillInfo],
)
async def list_conversation_skills(
    conversation_id: UUID,
    conversation_service: ConversationService = Depends(get_conversation_service),
) -> list[ConversationSkillInfo]:
    """List all skills for a conversation with their current activation state."""
    event_service = await conversation_service.get_event_service(conversation_id)
    if event_service is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    skills = event_service.get_skills()
    return [
        ConversationSkillInfo(
            name=item["skill"].name,
            source=item["skill"].source,
            description=item["skill"].description,
            always=item["skill"].always,
            triggers=item["skill"].triggers,
            state=item["state"],
        )
        for item in skills
    ]


@conversation_skills_router.get(
    "/conversations/{conversation_id}/skills/{skill_name}",
    response_model=ConversationSkillDetail,
)
async def get_conversation_skill(
    conversation_id: UUID,
    skill_name: str,
    conversation_service: ConversationService = Depends(get_conversation_service),
) -> ConversationSkillDetail:
    """Get a single skill with its current content (re-read from disk)."""
    event_service = await conversation_service.get_event_service(conversation_id)
    if event_service is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    skills = event_service.get_skills()
    match = next((item for item in skills if item["skill"].name == skill_name), None)
    if match is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Skill '{skill_name}' not found")
    return _to_detail(match["skill"], match["state"])


@conversation_skills_router.post(
    "/conversations/{conversation_id}/skills/{skill_name}/activate",
    response_model=ConversationSkillDetail,
)
async def activate_conversation_skill(
    conversation_id: UUID,
    skill_name: str,
    conversation_service: ConversationService = Depends(get_conversation_service),
) -> ConversationSkillDetail:
    """Promote a skill from discovered to active.

    Once active, the skill's content is included in <REPO_CONTEXT> on every
    agent turn (content re-read from disk). Returns 409 if the skill is already
    active, 404 if not found.
    """
    event_service = await conversation_service.get_event_service(conversation_id)
    if event_service is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    skill_info = await event_service.activate_skill(skill_name)
    if skill_info is None:
        # Could be: not found, already active, or no agent_context
        skills = event_service.get_skills()
        existing = next((i for i in skills if i["skill"].name == skill_name), None)
        if existing is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Skill '{skill_name}' not found")
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Skill '{skill_name}' is already active")
    return _to_detail(skill_info, "active")


@conversation_skills_router.post(
    "/conversations/{conversation_id}/skills/{skill_name}/deactivate",
    response_model=ConversationSkillDetail,
)
async def deactivate_conversation_skill(
    conversation_id: UUID,
    skill_name: str,
    conversation_service: ConversationService = Depends(get_conversation_service),
) -> ConversationSkillDetail:
    """Demote an active skill back to discovered.

    Returns 409 if already discovered or if the skill is always-on (cannot be
    deactivated). Returns 404 if not found.
    """
    event_service = await conversation_service.get_event_service(conversation_id)
    if event_service is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    skill_info = await event_service.deactivate_skill(skill_name)
    if skill_info is None:
        skills = event_service.get_skills()
        existing = next((i for i in skills if i["skill"].name == skill_name), None)
        if existing is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Skill '{skill_name}' not found")
        if existing["skill"].always:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Skill '{skill_name}' is always-on and cannot be deactivated")
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Skill '{skill_name}' is already discovered (not active)")
    return _to_detail(skill_info, "discovered")
