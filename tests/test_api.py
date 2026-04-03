import pytest

from claude_spend.api import AnthropicAPIError, build_cache_key, get_headers


def test_get_headers_with_missing_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_ADMIN_API_KEY", raising=False)
    with pytest.raises(AnthropicAPIError, match="Missing API key"):
        get_headers()


def test_get_headers_with_standard_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_ADMIN_API_KEY", "sk-ant-api-123")
    with pytest.raises(AnthropicAPIError, match="Admin API key"):
        get_headers()


def test_get_headers_with_admin_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_ADMIN_API_KEY", "sk-ant-admin-123456")
    headers = get_headers()
    assert headers["x-api-key"] == "sk-ant-admin-123456"
    assert headers["anthropic-version"] == "2023-06-01"


def test_build_cache_key_is_stable():
    params_a = {"starting_at": "2026-01-01", "limit": 31, "group_by[]": ["model"]}
    params_b = {"limit": 31, "group_by[]": ["model"], "starting_at": "2026-01-01"}

    key_a = build_cache_key("organizations/usage_report/messages", params_a)
    key_b = build_cache_key("organizations/usage_report/messages", params_b)

    assert key_a == key_b
