from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import time
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.repositories.postgres_repository import PostgresRepository  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure filtered HNSW recall and Dense/BM25 database latency."
    )
    parser.add_argument("--collection")
    parser.add_argument("--scope")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--ef-search", default="80,100,160")
    parser.add_argument("--max-scan-tuples", default="10000,20000,40000")
    parser.add_argument("--recall-min", type=float, default=0.95)
    parser.add_argument("--dense-p95-max-ms", type=float, default=150.0)
    parser.add_argument("--hybrid-p95-max-ms", type=float, default=250.0)
    parser.add_argument(
        "--allow-slo-failure",
        action="store_true",
        help="Print failed SLOs without returning a non-zero exit code.",
    )
    return parser.parse_args()


def parse_int_grid(raw: str) -> list[int]:
    values = sorted({int(item.strip()) for item in raw.split(",") if item.strip()})
    if not values or any(value <= 0 for value in values):
        raise ValueError("Benchmark parameter grids must contain positive integers.")
    return values


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return round(ordered[index], 3)


def row_value(row: Any, key: str) -> Any:
    return PostgresRepository._row_value(row, key)


def load_samples(
    repository: PostgresRepository,
    *,
    collection: str | None,
    scope: str | None,
    sample_count: int,
) -> tuple[str, str, int, list[dict[str, str]]]:
    with repository.connection() as conn:
        if collection and scope:
            target = {
                "collection_name": collection,
                "scope": scope,
            }
        else:
            row = conn.execute(
                """
                SELECT collection_name, scope, COUNT(*) AS row_count
                FROM kb_chunks
                GROUP BY collection_name, scope
                ORDER BY row_count DESC, collection_name, scope
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                raise RuntimeError("kb_chunks is empty; rebuild the knowledge base first.")
            target = {
                "collection_name": str(row_value(row, "collection_name")),
                "scope": str(row_value(row, "scope")),
            }
        rows = conn.execute(
            """
            SELECT chunk_id, text, embedding::text AS embedding,
                   vector_dims(embedding) AS dimension
            FROM kb_chunks
            WHERE collection_name = %s AND scope = %s
            ORDER BY md5(chunk_id)
            LIMIT %s
            """,
            (
                target["collection_name"],
                target["scope"],
                max(1, int(sample_count)),
            ),
        ).fetchall()
    if not rows:
        raise RuntimeError(
            f"No chunks found for collection={target['collection_name']!r}, "
            f"scope={target['scope']!r}."
        )
    dimensions = {int(row_value(row, "dimension")) for row in rows}
    if len(dimensions) != 1:
        raise RuntimeError(f"Sampled chunks contain mixed dimensions: {sorted(dimensions)}")
    samples = [
        {
            "chunk_id": str(row_value(row, "chunk_id")),
            "text": str(row_value(row, "text")),
            "embedding": str(row_value(row, "embedding")),
        }
        for row in rows
    ]
    return (
        target["collection_name"],
        target["scope"],
        dimensions.pop(),
        samples,
    )


def distance_expression(dimension: int) -> str:
    vector_type = "halfvec" if dimension > 2000 else "vector"
    return (
        f"embedding::{vector_type}({dimension}) "
        f"<=> %s::{vector_type}({dimension})"
    )


def exact_ids(
    repository: PostgresRepository,
    *,
    collection: str,
    scope: str,
    dimension: int,
    embedding: str,
    top_k: int,
) -> list[str]:
    distance = distance_expression(dimension)
    with repository.connection() as conn:
        conn.execute("SET LOCAL enable_indexscan = off")
        conn.execute("SET LOCAL enable_bitmapscan = off")
        rows = conn.execute(
            f"""
            SELECT chunk_id
            FROM kb_chunks
            WHERE collection_name = %s
              AND scope = %s
              AND vector_dims(embedding) = {dimension}
            ORDER BY {distance}
            LIMIT %s
            """,
            (collection, scope, embedding, int(top_k)),
        ).fetchall()
    return [str(row_value(row, "chunk_id")) for row in rows]


def ann_and_hybrid(
    repository: PostgresRepository,
    *,
    collection: str,
    scope: str,
    dimension: int,
    query_text: str,
    embedding: str,
    top_k: int,
    ef_search: int,
    max_scan_tuples: int,
) -> tuple[list[str], float, float]:
    distance = distance_expression(dimension)
    tokenizer = str(repository.settings.postgres_bm25_tokenizer_name)
    bm25_index = repository.bm25_index_name(collection, scope)
    with repository.connection() as conn:
        conn.execute("SET LOCAL enable_seqscan = off")
        conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        conn.execute(f"SET LOCAL hnsw.ef_search = {int(ef_search)}")
        conn.execute(
            f"SET LOCAL hnsw.max_scan_tuples = {int(max_scan_tuples)}"
        )
        conn.execute(
            f"SET LOCAL bm25_catalog.bm25_limit = "
            f"{int(repository.settings.postgres_bm25_limit)}"
        )

        hybrid_started = time.perf_counter()
        dense_started = time.perf_counter()
        dense_rows = conn.execute(
            f"""
            SELECT chunk_id
            FROM kb_chunks
            WHERE collection_name = %s
              AND scope = %s
              AND vector_dims(embedding) = {dimension}
            ORDER BY {distance}
            LIMIT %s
            """,
            (collection, scope, embedding, int(top_k)),
        ).fetchall()
        dense_ms = (time.perf_counter() - dense_started) * 1000

        conn.execute(
            f"""
            SELECT chunk_id
            FROM kb_chunks
            WHERE collection_name = %s
              AND scope = %s
              AND bm25_embedding IS NOT NULL
            ORDER BY bm25_embedding <&> bm25_catalog.to_bm25query(
                'public.{bm25_index}'::regclass,
                tokenizer_catalog.tokenize(%s, %s)
            )
            LIMIT %s
            """,
            (collection, scope, query_text, tokenizer, int(top_k)),
        ).fetchall()
        hybrid_ms = (time.perf_counter() - hybrid_started) * 1000
    return (
        [str(row_value(row, "chunk_id")) for row in dense_rows],
        dense_ms,
        hybrid_ms,
    )


def main() -> int:
    args = parse_args()
    repository = PostgresRepository()
    collection, scope, dimension, samples = load_samples(
        repository,
        collection=args.collection,
        scope=args.scope,
        sample_count=args.samples,
    )
    top_k = max(1, int(args.top_k))
    exact_results = [
        exact_ids(
            repository,
            collection=collection,
            scope=scope,
            dimension=dimension,
            embedding=sample["embedding"],
            top_k=top_k,
        )
        for sample in samples
    ]

    results: list[dict[str, Any]] = []
    for ef_search in parse_int_grid(args.ef_search):
        for max_scan_tuples in parse_int_grid(args.max_scan_tuples):
            recalls: list[float] = []
            dense_latencies: list[float] = []
            hybrid_latencies: list[float] = []
            for sample, expected_ids in zip(samples, exact_results, strict=True):
                actual_ids, dense_ms, hybrid_ms = ann_and_hybrid(
                    repository,
                    collection=collection,
                    scope=scope,
                    dimension=dimension,
                    query_text=sample["text"],
                    embedding=sample["embedding"],
                    top_k=top_k,
                    ef_search=ef_search,
                    max_scan_tuples=max_scan_tuples,
                )
                denominator = max(1, min(top_k, len(expected_ids)))
                recalls.append(len(set(expected_ids) & set(actual_ids)) / denominator)
                dense_latencies.append(dense_ms)
                hybrid_latencies.append(hybrid_ms)

            recall_at_k = round(sum(recalls) / len(recalls), 4)
            dense_p95 = percentile(dense_latencies, 0.95)
            hybrid_p95 = percentile(hybrid_latencies, 0.95)
            result = {
                "ef_search": ef_search,
                "max_scan_tuples": max_scan_tuples,
                f"recall_at_{top_k}": recall_at_k,
                "dense_p95_ms": dense_p95,
                "hybrid_database_p95_ms": hybrid_p95,
                "meets_recall_slo": recall_at_k >= args.recall_min,
                "meets_dense_latency_slo": dense_p95 <= args.dense_p95_max_ms,
                "meets_hybrid_latency_slo": hybrid_p95
                <= args.hybrid_p95_max_ms,
            }
            result["meets_all_slos"] = all(
                value
                for key, value in result.items()
                if key.startswith("meets_") and key != "meets_all_slos"
            )
            results.append(result)

    report = {
        "collection_name": collection,
        "scope": scope,
        "dimension": dimension,
        "sample_count": len(samples),
        "top_k": top_k,
        "thresholds": {
            "recall_min": args.recall_min,
            "dense_p95_max_ms": args.dense_p95_max_ms,
            "hybrid_p95_max_ms": args.hybrid_p95_max_ms,
        },
        "results": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    passed = any(bool(result["meets_all_slos"]) for result in results)
    return 0 if passed or args.allow_slo_failure else 1


if __name__ == "__main__":
    raise SystemExit(main())
