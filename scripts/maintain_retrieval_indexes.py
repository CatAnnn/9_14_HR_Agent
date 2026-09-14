from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config.settings import get_settings  # noqa: E402
from backend.repositories.postgres_repository import PostgresRepository  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build partial HNSW/BM25 indexes outside the request path."
    )
    parser.add_argument(
        "--skip-lexical-rebuild",
        action="store_true",
        help="Keep the current tokenizer/model/trigger and BM25 vector values.",
    )
    parser.add_argument(
        "--keep-legacy-hnsw",
        action="store_true",
        help="Do not drop the old dimension-only HNSW indexes after validation.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    repository = PostgresRepository(
        database_url=get_settings().admin_database_url,
    )
    summary = repository.maintain_retrieval_indexes(
        rebuild_lexical=not args.skip_lexical_rebuild,
        drop_legacy_hnsw=not args.keep_legacy_hnsw,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
