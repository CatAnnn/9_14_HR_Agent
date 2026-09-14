from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.main import _wait_for_active_embedding_profile


def _profile(*, ready: bool):
    return SimpleNamespace(
        ready=ready,
        profile_id="profile-id",
        active_build_id="build-id" if ready else None,
        vector_count=900 if ready else 827,
        status="ready" if ready else "stale",
        stale_reason=None if ready else "knowledge_fingerprint_changed",
    )


@pytest.mark.asyncio
async def test_wait_for_active_embedding_profile_observes_atomic_activation():
    states = iter([_profile(ready=False), _profile(ready=True)])

    class Repository:
        def embedding_profile_state(self, model_name, dimensions):
            assert model_name == "text-embedding-v4"
            assert dimensions == 2048
            return next(states)

    state = await _wait_for_active_embedding_profile(
        Repository(),
        model_name="text-embedding-v4",
        dimensions=2048,
        timeout_seconds=1,
        poll_interval_seconds=0,
    )

    assert state.ready is True


@pytest.mark.asyncio
async def test_wait_for_active_embedding_profile_times_out_with_state_details():
    class Repository:
        def embedding_profile_state(self, model_name, dimensions):
            return _profile(ready=False)

    with pytest.raises(RuntimeError) as exc_info:
        await _wait_for_active_embedding_profile(
            Repository(),
            model_name="text-embedding-v4",
            dimensions=2048,
            timeout_seconds=0,
        )

    message = str(exc_info.value)
    assert "profile_id=profile-id" in message
    assert "status=stale" in message
    assert "reason=knowledge_fingerprint_changed" in message
