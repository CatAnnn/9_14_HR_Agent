from __future__ import annotations

import hashlib
import unicodedata
from typing import Any, Literal, Mapping


EmbeddingCorpus = Literal["all", "kb_raw"]

# A profile is still identified only by its normalized model name and
# dimensions. This mapping controls which canonical knowledge rows belong to
# that vector space; provider and endpoint never participate in the decision.
_KB_RAW_ONLY_PROFILES = frozenset({("text-embedding-v4", 2048)})


def normalize_embedding_model_name(model_name: str) -> str:
    """Return the canonical model name used to identify one vector space."""

    normalized = unicodedata.normalize("NFKC", str(model_name or "")).strip()
    if not normalized:
        raise ValueError("Embedding model name cannot be empty.")
    return normalized


def normalize_embedding_dimensions(dimensions: int) -> int:
    try:
        normalized = int(dimensions)
    except (TypeError, ValueError) as exc:
        raise ValueError("Embedding dimensions must be a positive integer.") from exc
    if normalized <= 0:
        raise ValueError("Embedding dimensions must be a positive integer.")
    return normalized


def resolve_embedding_dimensions(settings: object) -> int:
    """Resolve the active dimension while accepting legacy settings doubles."""

    dimensions = getattr(
        settings,
        "effective_embedding_dimensions",
        None,
    )
    if dimensions is None:
        dimensions = getattr(settings, "embedding_dimensions", None)
    return normalize_embedding_dimensions(dimensions)


def embedding_profile_id(model_name: str, dimensions: int) -> str:
    """Build the stable identity for a model-compatible vector repository."""

    model = normalize_embedding_model_name(model_name)
    dimension = normalize_embedding_dimensions(dimensions)
    return hashlib.sha256(f"{model}:{dimension}".encode("utf-8")).hexdigest()


def embedding_profile_corpus(
    model_name: str,
    dimensions: int,
) -> EmbeddingCorpus:
    """Return the canonical knowledge corpus selected by one vector profile."""

    identity = (
        normalize_embedding_model_name(model_name),
        normalize_embedding_dimensions(dimensions),
    )
    return "kb_raw" if identity in _KB_RAW_ONLY_PROFILES else "all"


def embedding_metadata_in_corpus(
    metadata: Mapping[str, Any] | None,
    corpus: EmbeddingCorpus,
) -> bool:
    """Check whether one canonical chunk belongs to the selected corpus."""

    if corpus == "all":
        return True
    source_path = str((metadata or {}).get("source_path") or "").replace(
        "\\", "/"
    )
    return "/data/kb_raw/" in source_path
