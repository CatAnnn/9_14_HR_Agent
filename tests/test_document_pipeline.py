import sys
import threading
import types

import pytest

from backend.exceptions.parser_errors import UnsupportedFileTypeError
from backend.parsers.parser_router import ParserRouter
from backend.services.document_pipeline import DocumentPipeline


def test_upload_document_pipeline_uses_markitdown(monkeypatch, tmp_path):
    calls = []

    class FakeResult:
        text_content = "# 员工档案\n\n张三，绩效 4。"

    class FakeMarkItDown:
        def convert(self, path):
            calls.append(path)
            return FakeResult()

    monkeypatch.setitem(sys.modules, "markitdown", types.SimpleNamespace(MarkItDown=FakeMarkItDown))
    source = tmp_path / "profile.docx"
    source.write_bytes(b"fake docx content")

    parsed = DocumentPipeline().parse_file(source, document_id="doc-1")

    assert parsed.text == "# 员工档案\n\n张三，绩效 4。"
    assert parsed.filename == "profile.docx"
    assert parsed.metadata["parser"] == "markitdown"
    assert parsed.metadata["source_format"] == "docx"
    assert parsed.metadata["text_chars"] == len(parsed.text)
    assert calls == [str(source)]


def test_upload_document_pipeline_rejects_unsupported_file(tmp_path):
    source = tmp_path / "profile.exe"
    source.write_bytes(b"not a document")

    with pytest.raises(UnsupportedFileTypeError):
        DocumentPipeline().parse_file(source, document_id="doc-1")


def test_upload_document_pipeline_keeps_parser_metadata_request_local(monkeypatch, tmp_path):
    parsers = []

    class FakeParser:
        def __init__(self):
            self.last_metadata = {"parser": "fake"}
            parsers.append(self)

        def parse(self, path):
            self.last_metadata["input_path"] = str(path)
            return path.stem

    monkeypatch.setattr("backend.services.document_pipeline.MarkItDownParser", FakeParser)
    pipeline = DocumentPipeline()
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"

    first_result = pipeline.parse_file(first, document_id="first")
    second_result = pipeline.parse_file(second, document_id="second")

    assert first_result.metadata["input_path"] == str(first)
    assert second_result.metadata["input_path"] == str(second)
    assert len(parsers) == 2
    assert parsers[0] is not parsers[1]


def test_kb_parser_router_rejects_raw_document_files(tmp_path):
    router = ParserRouter()
    source = tmp_path / "kb.pdf"
    source.write_bytes(b"fake pdf content")

    with pytest.raises(UnsupportedFileTypeError, match="processed Markdown"):
        router.parse_file(source)


@pytest.mark.asyncio
async def test_upload_document_service_stores_text_as_session_supplemental_info():
    from backend.schemas.profile import EmployeeProfile
    from backend.schemas.state import SessionState
    from backend.services.upload_document import UploadDocumentService

    class FakeRepo:
        def __init__(self):
            self.saved = None

        def new_id(self):
            return "doc-1"

        def save(self, record):
            self.saved = record

    class FakeSessionService:
        def __init__(self, state):
            self.state = state

        def get_session(self, session_id):
            assert session_id == self.state.session_id
            return self.state

        def save_session(self, state):
            self.state = state
            return state

    state = SessionState(
        session_id="s1",
        employee_profile=EmployeeProfile(
            employee_alias="员工A",
            role="工程师",
            performance_rating="4",
            review_cycle="2026",
            conversation_topic="绩效反馈",
            key_goals=["交付质量"],
        ),
    )
    service = UploadDocumentService()
    service.repo = FakeRepo()
    service.session_service = FakeSessionService(state)

    record = await service.process_text("员工最近担心奖金受影响", filename="note.txt", session_id="s1")

    assert record.profile is state.employee_profile
    assert record.profile.employee_alias == "员工A"
    assert record.profile.supplemental_info is None
    assert "note.txt" in (state.supplemental_info or "")
    assert "员工最近担心奖金受影响" in (state.supplemental_info or "")
    assert record.metadata["usage"] == "session_supplemental_info"
    assert service.repo.saved is record

@pytest.mark.asyncio
async def test_upload_document_service_stores_file_text_with_original_filename(tmp_path):
    from backend.schemas.document import ParsedDocument
    from backend.schemas.profile import EmployeeProfile
    from backend.schemas.state import SessionState
    from backend.services.upload_document import UploadDocumentService

    class FakeRepo:
        def __init__(self):
            self.saved = None

        def new_id(self):
            return "doc-2"

        def save(self, record):
            self.saved = record

    caller_thread_id = threading.get_ident()
    parser_thread_ids = []

    class FakePipeline:
        def parse_file(self, path, document_id):
            parser_thread_ids.append(threading.get_ident())
            assert path.name == "uuid_profile.docx"
            return ParsedDocument(
                document_id=document_id,
                filename=path.name,
                text="员工完整补充资料全文",
                metadata={"parser": "markitdown"},
            )

    class FakeSessionService:
        def __init__(self, state):
            self.state = state

        def get_session(self, session_id):
            assert session_id == self.state.session_id
            return self.state

        def save_session(self, state):
            self.state = state
            return state

    state = SessionState(session_id="s2")
    service = UploadDocumentService()
    service.repo = FakeRepo()
    service.pipeline = FakePipeline()
    service.session_service = FakeSessionService(state)

    source = tmp_path / "uuid_profile.docx"
    source.write_bytes(b"fake")

    record = await service.process_file(source, session_id="s2", filename="profile.docx")

    assert record.filename == "profile.docx"
    assert record.raw_path == str(source)
    assert record.parsed_text == "员工完整补充资料全文"
    assert record.profile is None
    assert state.employee_profile is None
    assert "profile.docx" in (state.supplemental_info or "")
    assert "uuid_profile.docx" not in (state.supplemental_info or "")
    assert "员工完整补充资料全文" in (state.supplemental_info or "")
    assert record.metadata["usage"] == "session_supplemental_info"
    assert parser_thread_ids and parser_thread_ids[0] != caller_thread_id
