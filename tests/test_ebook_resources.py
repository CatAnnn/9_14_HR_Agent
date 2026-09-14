from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.testclient import TestClient
import pytest

from backend.api.routes import ebooks
from backend.core.auth_dependency import get_current_session, get_current_user
from backend.schemas.auth import AuthUserResponse
from backend.services.auth_session_service import AuthSession
from backend.services.ebook_catalog_service import (
    EbookCatalogError,
    EbookCatalogService,
)


EXPECTED_EBOOK_IDS = [
    "nonviolent-communication",
    "the-art-of-communication",
    "never-split-the-difference",
    "harvard-classic-negotiation",
    "conflict-resolution-skills",
    "influence",
    "ted-talks-speaking",
    "deliberate-practice",
    "enneagram",
]


@pytest.fixture(scope="module")
def catalog() -> EbookCatalogService:
    return EbookCatalogService()


def _auth_session() -> AuthSession:
    return AuthSession(
        session_id="ebook-test-session",
        user=AuthUserResponse(
            id="ebook-test-user",
            email="reader@bosch.com",
            display_name="Reader",
            role="user",
        ),
        user_id="ebook-test-user",
        role="user",
        created_at=0,
        last_seen_at=0,
    )


def _app(catalog: EbookCatalogService, *, authenticated: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(
        ebooks.router,
        prefix="/api/v1",
        dependencies=[Depends(get_current_user)],
    )
    app.dependency_overrides[ebooks.get_ebook_catalog_service] = lambda: catalog
    if authenticated:
        app.dependency_overrides[get_current_session] = _auth_session
    else:
        def reject_authentication() -> None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
            )

        app.dependency_overrides[get_current_user] = reject_authentication
    return app


def test_catalog_contains_all_valid_epubs_in_required_order(
    catalog: EbookCatalogService,
) -> None:
    records = catalog.all()

    assert [record.id for record in records] == EXPECTED_EBOOK_IDS
    assert all(record.file_path.is_file() for record in records)
    assert all(record.cover_path.is_file() for record in records)
    assert all("db" not in record.title.casefold() for record in records)


def test_catalog_rejects_source_paths_outside_ebook_directory(tmp_path: Path) -> None:
    ebook_root = tmp_path / "Ebook"
    ebook_root.mkdir()
    (ebook_root / "catalog.json").write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {
                        "id": "unsafe-book",
                        "source_file": "../outside.epub",
                        "title": "Unsafe",
                        "authors": ["Author"],
                        "publisher": None,
                        "published_at": None,
                        "summary": "Summary",
                        "tags": ["Tag"],
                        "cover": "/assets/resource/ebooks/covers/unsafe.jpg",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(EbookCatalogError, match="inside Ebook"):
        EbookCatalogService(project_root=tmp_path)


def test_ebook_file_requires_authentication(catalog: EbookCatalogService) -> None:
    with TestClient(_app(catalog, authenticated=False)) as client:
        response = client.get(
            "/api/v1/resources/ebooks/nonviolent-communication/file"
        )

    assert response.status_code == 401


def test_ebook_file_supports_head_range_and_private_cache(
    catalog: EbookCatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ebooks, "record_ebook_event", lambda *args, **kwargs: None)
    with TestClient(_app(catalog)) as client:
        head = client.head(
            "/api/v1/resources/ebooks/nonviolent-communication/file"
        )
        partial = client.get(
            "/api/v1/resources/ebooks/nonviolent-communication/file",
            headers={"Range": "bytes=0-31"},
        )

    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-type"].startswith("application/epub+zip")
    assert head.headers["cache-control"].startswith("private")
    assert head.headers["accept-ranges"] == "bytes"
    assert partial.status_code == 206
    assert len(partial.content) == 32
    assert partial.headers["content-range"].startswith("bytes 0-31/")


def test_download_matches_source_sha256_and_uses_ascii_filename(
    catalog: EbookCatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ebooks, "record_ebook_event", lambda *args, **kwargs: None)
    record = catalog.require("the-art-of-communication")
    with TestClient(_app(catalog)) as client:
        response = client.get(
            "/api/v1/resources/ebooks/the-art-of-communication/file?download=true"
        )

    assert response.status_code == 200
    assert 'attachment; filename="the-art-of-communication.epub"' in response.headers[
        "content-disposition"
    ]
    assert hashlib.sha256(response.content).digest() == hashlib.sha256(
        record.file_path.read_bytes()
    ).digest()


def test_unknown_ebook_id_returns_404_without_path_access(
    catalog: EbookCatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ebooks, "record_ebook_event", lambda *args, **kwargs: None)
    with TestClient(_app(catalog)) as client:
        response = client.get("/api/v1/resources/ebooks/not-in-catalog/file")

    assert response.status_code == 404
    assert "Ebook" not in response.text
    assert str(catalog.project_root) not in response.text
