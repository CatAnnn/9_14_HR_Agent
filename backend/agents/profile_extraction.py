from __future__ import annotations

from backend.schemas.profile import EmployeeProfile, EmployeeProfileExtractionOutput
from backend.services.langchain_llm_service import LangChainLLMService
from backend.services.prompt_service import PromptService
from backend.utils.logger import get_logger

logger = get_logger(__name__)


class ProfileExtractionAgent:
    """Employee profile extraction via LangChain structured output only."""

    async def extract(self, document_text: str) -> EmployeeProfile:
        return await self._extract_with_llm(document_text)

    async def _extract_with_llm(self, document_text: str) -> EmployeeProfile:
        logger.info("profile.extract.start text_chars=%s", len(document_text or ""))
        prompt = PromptService().render("profile/extraction.jinja2", document_text=document_text)
        output = await LangChainLLMService().ainvoke_structured(
            prompt=prompt,
            schema=EmployeeProfileExtractionOutput,
            task_name="profile",
        )
        profile = EmployeeProfile.model_validate(output.model_dump(mode="json"))
        if not profile.employee_alias:
            raise ValueError("Profile extraction failed: employee_alias is missing from structured_response.")
        logger.info("profile.extract.done employee_alias_set=%s ready=%s", bool(profile.employee_alias), profile.is_ready_for_setup())
        return profile
