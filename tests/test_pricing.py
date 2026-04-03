from claude_spend.pricing import calc_cache_savings, calc_cost, get_model_pricing


def test_get_model_pricing_known_model():
    pricing = get_model_pricing("claude-sonnet-4-6")
    assert pricing["input"] == 3.00
    assert pricing["output"] == 15.00


def test_get_model_pricing_unknown_model_falls_back():
    pricing = get_model_pricing("unknown-model")
    assert pricing["input"] == 3.00
    assert pricing["output"] == 15.00


def test_calc_cost_basic():
    cost = calc_cost(
        model="claude-sonnet-4-6",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=0,
        cache_write_tokens=0,
    )
    assert cost == 18.0


def test_calc_cost_with_cache():
    cost = calc_cost(
        model="claude-sonnet-4-6",
        input_tokens=1_000_000,
        output_tokens=500_000,
        cache_read_tokens=500_000,
        cache_write_tokens=250_000,
    )
    expected = 3.0 + 7.5 + 0.15 + 0.9375
    assert round(cost, 6) == round(expected, 6)


def test_calc_cache_savings():
    savings = calc_cache_savings("claude-sonnet-4-6", 1_000_000)
    assert savings == 2.7
