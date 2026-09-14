from __future__ import annotations

from backend.vectorstore.index_manager import IndexManager


class KBIngestionService:
    def __init__(self):
        self.index_manager = IndexManager()

    def ingest(self) -> dict:
        chunks = self.index_manager.rebuild()
        summary = dict(self.index_manager.last_summary)
        summary.setdefault("chunk_count", len(chunks))
        return summary
