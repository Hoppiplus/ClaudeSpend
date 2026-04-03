from __future__ import annotations

PRICING = {
    "claude-opus-4-6": {
        "input": 5.00,
        "output": 25.00,
        "cache_write_5min": 6.25,
        "cache_write_1hr": 10.00,
        "cache_read": 0.50,
    },
    "claude-opus-4-5": {
        "input": 5.00,
        "output": 25.00,
        "cache_write_5min": 6.25,
        "cache_write_1hr": 10.00,
        "cache_read": 0.50,
    },
    "claude-sonnet-4-6": {
        "input": 3.00,
        "output": 15.00,
        "cache_write_5min": 3.75,
        "cache_write_1hr": 6.00,
        "cache_read": 0.30,
    },
    "claude-sonnet-4-5": {
        "input": 3.00,
        "output": 15.00,
        "cache_write_5min": 3.75,
        "cache_write_1hr": 6.00,
        "cache_read": 0.30,
    },
    "claude-haiku-4-5": {
        "input": 1.00,
        "output": 5.00,
        "cache_write_5min": 1.25,
        "cache_write_1hr": 2.00,
        "cache_read": 0.10,
    },
    "claude-haiku-3-5": {
        "input": 0.80,
        "output": 4.00,
        "cache_write_5min": 1.00,
        "cache_write_1hr": 1.60,
        "cache_read": 0.08,
    },
}

DEFAULT_MODEL = "claude-sonnet-4-6"
MTOK = 1_000_000


def get_model_pricing(model: str) -> dict:
    return PRICING.get(model, PRICING[DEFAULT_MODEL])


def calc_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    p = get_model_pricing(model)
    cost = (
        (input_tokens / MTOK) * p["input"]
        + (output_tokens / MTOK) * p["output"]
        + (cache_read_tokens / MTOK) * p["cache_read"]
        + (cache_write_tokens / MTOK) * p["cache_write_5min"]
    )
    return round(cost, 6)


def calc_cache_savings(model: str, cache_read_tokens: int) -> float:
    p = get_model_pricing(model)
    full_price = (cache_read_tokens / MTOK) * p["input"]
    cache_price = (cache_read_tokens / MTOK) * p["cache_read"]
    return round(full_price - cache_price, 6)
