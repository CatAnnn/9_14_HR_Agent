from __future__ import annotations

from backend.config.settings import Settings, get_settings
from backend.services.session_service import SessionService
from backend.services.setup_service import SetupService
from backend.services.upload_document import UploadDocumentService
from backend.services.guidance_service import GuidanceService
from backend.services.rehearsal_service import RehearsalService
from backend.services.resource_chat_service import ResourceChatService
from backend.services.coach_service import CoachService
from backend.services.speech_websocket_service import SpeechWebSocketHub
from backend.services.employee_database_service import EmployeeDatabaseService
from backend.services.application_runtime import get_application_runtime


def get_app_settings() -> Settings:
    return get_settings()


def get_session_service() -> SessionService:
    return get_application_runtime().session_service


def get_setup_service() -> SetupService:
    return get_application_runtime().setup_service


def get_document_service() -> UploadDocumentService:
    return get_application_runtime().document_service


def get_employee_database_service() -> EmployeeDatabaseService:
    return get_application_runtime().employee_database_service


def get_guidance_service() -> GuidanceService:
    return get_application_runtime().guidance_service


def get_rehearsal_service() -> RehearsalService:
    return get_application_runtime().rehearsal_service


def get_speech_websocket_hub() -> SpeechWebSocketHub:
    return get_application_runtime().speech_websocket_hub


def get_coach_service() -> CoachService:
    return get_application_runtime().coach_service


def get_resource_chat_service() -> ResourceChatService:
    return get_application_runtime().resource_chat_service
