from __future__ import annotations

from pydantic import BaseModel

from backend.business_config.loader import get_config_loader
from backend.schemas.intent import IntentResult
from backend.schemas.profile import EmployeeProfile
from backend.services.langchain_llm_service import LangChainLLMService
from backend.services.prompt_service import PromptService


class IntentRecognitionStructuredOutput(BaseModel):
    intent_id: str


class IntentRecognitionAgent:
    async def recognize(self, text: str | None = None, profile: EmployeeProfile | None = None, intent_id: str | None = None) -> IntentResult:
        loader = get_config_loader()
        intents = loader.intents()
        if intent_id and intent_id in intents:
            return IntentResult(intent_id=intent_id, confidence=1.0, reason="用户明确选择", config=intents[intent_id])
        result = await self._recognize_with_llm(text=text, profile=profile, valid_intents=list(intents.keys()))
        if result.intent_id not in intents:
            raise ValueError(f"Intent recognition returned unsupported intent_id: {result.intent_id}")
        result.config = intents[result.intent_id]
        return result

    async def _recognize_with_llm(self, text: str | None, profile: EmployeeProfile | None, valid_intents: list[str]) -> IntentResult:
        user_text = "\n".join(
            filter(
                None,
                [
                    text,
                    profile.conversation_topic if profile else None,
                    profile.model_dump_json(exclude_none=True) if profile else None,
                ],
            )
        )
        prompt = PromptService().render("intent/recognition.jinja2", intent_ids=valid_intents, user_text=user_text)
        output = await LangChainLLMService().ainvoke_structured(
            prompt=prompt,
            schema=IntentRecognitionStructuredOutput,
            task_name="intent",
        )
        return IntentResult(intent_id=output.intent_id)
