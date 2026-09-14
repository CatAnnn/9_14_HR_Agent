from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping, Protocol


class RedisLike(Protocol):
    def eval(self, script: str, numkeys: int, *values: object) -> object: ...


@dataclass(frozen=True, slots=True)
class TokenRates:
    input_per_million: float
    output_per_million: float


DEFAULT_RATES: dict[str, TokenRates] = {
    "deepseek-v4-pro": TokenRates(12.0, 24.0),
    "qwen3.7-max": TokenRates(12.0, 36.0),
    "qwen3.7-plus": TokenRates(2.0, 8.0),
    "qwen3.6-flash": TokenRates(1.2, 7.2),
    "doubao-seed-evolving": TokenRates(6.0, 30.0),
    "doubao-seed-2.1-pro": TokenRates(6.0, 30.0),
    "doubao-seed-2.1-turbo": TokenRates(3.0, 15.0),
    "glm-5.2": TokenRates(8.0, 28.0),
}


def parse_rates(raw: str | None) -> dict[str, TokenRates]:
    if not raw:
        return dict(DEFAULT_RATES)
    payload = json.loads(raw)
    rates = dict(DEFAULT_RATES)
    for model, values in payload.items():
        rates[str(model)] = TokenRates(
            input_per_million=float(values["input_per_million"]),
            output_per_million=float(values["output_per_million"]),
        )
    return rates


def estimate_cost_cny(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    request_count: int = 1,
    rates: Mapping[str, TokenRates] | None = None,
) -> float:
    selected = (rates or DEFAULT_RATES).get(model)
    if selected is None:
        raise ValueError(f"no CNY token pricing configured for model {model!r}")
    if min(input_tokens, output_tokens, request_count) < 0:
        raise ValueError("token and request counts cannot be negative")
    per_request = (
        input_tokens * selected.input_per_million
        + output_tokens * selected.output_per_million
    ) / 1_000_000.0
    return per_request * request_count


_RESERVE_COST_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local addition = tonumber(ARGV[1])
local maximum = tonumber(ARGV[2])
if current + addition > maximum then
  return {0, tostring(current)}
end
local updated = redis.call('INCRBYFLOAT', KEYS[1], addition)
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return {1, tostring(updated)}
"""


def reserve_cost(
    redis_client: RedisLike,
    *,
    key: str,
    estimated_cny: float,
    maximum_cny: float,
    ttl_seconds: int = 86_400,
) -> float:
    if estimated_cny < 0 or maximum_cny <= 0:
        raise ValueError("cost reservation values are invalid")
    result = redis_client.eval(
        _RESERVE_COST_LUA,
        1,
        key,
        f"{estimated_cny:.12f}",
        f"{maximum_cny:.12f}",
        ttl_seconds,
    )
    allowed_raw, total_raw = result  # type: ignore[misc]
    if int(allowed_raw) != 1:
        raise RuntimeError(
            f"live-model cost ceiling would be exceeded: reserved={float(total_raw):.4f} CNY, "
            f"next={estimated_cny:.4f} CNY, maximum={maximum_cny:.4f} CNY"
        )
    return float(total_raw)

