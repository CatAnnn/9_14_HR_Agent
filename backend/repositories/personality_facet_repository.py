from __future__ import annotations

from backend.repositories.postgres_repository import PostgresRepository
from backend.schemas.personality_facets import PersonalityFacetState


class PersonalityFacetRepository:
    """Internal, cross-session cache for stable employee facet profiles."""

    def __init__(self, repository: PostgresRepository | None = None):
        self.repo = repository or PostgresRepository()

    def get(self, profile_key: str) -> PersonalityFacetState | None:
        with self.repo.connection() as conn:
            row = conn.execute(
                """
                SELECT facets_json
                FROM personality_facet_profiles
                WHERE profile_key = %s
                """,
                (profile_key,),
            ).fetchone()
        if row is None:
            return None
        return PersonalityFacetState.model_validate(row["facets_json"])

    def save_or_get(
        self,
        *,
        profile_key: str,
        employee_key_hash: str,
        parent_scores: dict[str, int],
        facets: PersonalityFacetState,
    ) -> PersonalityFacetState:
        with self.repo.connection() as conn:
            conn.execute(
                """
                INSERT INTO personality_facet_profiles (
                    profile_key,
                    employee_key_hash,
                    parent_scores,
                    facets_json,
                    source,
                    generator_version,
                    model_name,
                    updated_at
                )
                VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, NOW())
                ON CONFLICT (profile_key) DO NOTHING
                """,
                (
                    profile_key,
                    employee_key_hash,
                    self.repo.dumps(parent_scores),
                    self.repo.dumps(facets.model_dump(mode="json")),
                    facets.source,
                    facets.generator_version,
                    facets.model_name,
                ),
            )
            row = conn.execute(
                """
                SELECT facets_json
                FROM personality_facet_profiles
                WHERE profile_key = %s
                """,
                (profile_key,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Personality facet profile was not persisted.")
        return PersonalityFacetState.model_validate(row["facets_json"])
