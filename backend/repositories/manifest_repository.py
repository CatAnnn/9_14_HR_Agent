from __future__ import annotations

from backend.repositories.postgres_repository import PostgresRepository


class ManifestRepository:
    def __init__(self, repository: PostgresRepository | None = None):
        self._repo = repository
        self.key = "kb_manifest"

    @property
    def repo(self) -> PostgresRepository:
        if self._repo is None:
            self._repo = PostgresRepository()
        return self._repo

    def load(self) -> dict:
        return self.repo.load_metadata(self.key) or {"documents": []}

    def save(self, manifest: dict) -> dict:
        return self.repo.save_metadata(self.key, manifest)
