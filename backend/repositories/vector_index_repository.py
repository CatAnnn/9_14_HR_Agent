from __future__ import annotations

from backend.repositories.postgres_repository import PostgresRepository


class VectorIndexRepository:
    def __init__(self, repository: PostgresRepository | None = None):
        self._repo = repository
        self.key = "vector_index_metadata"

    @property
    def repo(self) -> PostgresRepository:
        if self._repo is None:
            self._repo = PostgresRepository()
        return self._repo

    def load(self) -> dict:
        return self.repo.load_metadata(self.key)

    def save(self, data: dict) -> dict:
        return self.repo.save_metadata(self.key, data)
