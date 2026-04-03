from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional

import httpx
from dotenv import load_dotenv

from .db import get_cached, set_cache

load_dotenv()

BASE_URL = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3


class AnthropicAPIError(Exception):
    pass


def get_headers() -> Dict[str, str]:
    api_key = os.getenv("ANTHROPIC_ADMIN_API_KEY")

    if not api_key:
        raise AnthropicAPIError("Missing API key")

    if not api_key.startswith("sk-ant-admin"):
        raise AnthropicAPIError(
            "❌ This requires an Admin API key (sk-ant-admin...). Get one at console.anthropic.com → Settings → Admin Keys"
        )

    return {
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
    }


def _normalize_for_cache(params: Dict[str, Any]) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"))


def build_cache_key(endpoint: str, params: Dict[str, Any]) -> str:
    raw = f"{endpoint}:{_normalize_for_cache(params)}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"anthropic:{digest}"


def _flatten_params(params: Dict[str, Any]) -> List[tuple[str, str]]:
    flat: List[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            for item in value:
                flat.append((key, str(item)))
        else:
            flat.append((key, str(value)))
    return flat


async def _request_json(
    client: httpx.AsyncClient,
    endpoint: str,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    headers = get_headers()
    url = f"{BASE_URL}/{endpoint}"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = await client.get(url, headers=headers, params=_flatten_params(params))
        except httpx.RequestError as exc:
            if attempt == MAX_RETRIES:
                raise AnthropicAPIError("🌐 Can't reach Anthropic API. Check your internet connection.") from exc
            await asyncio.sleep(attempt)
            continue

        if response.status_code == 401:
            raise AnthropicAPIError("❌ Invalid API key. Check your ANTHROPIC_ADMIN_API_KEY in .env")

        if response.status_code == 403:
            raise AnthropicAPIError(
                "❌ Admin API requires an organization account. Set up your org at Console → Settings → Organization"
            )

        if response.status_code == 429:
            if attempt == MAX_RETRIES:
                raise AnthropicAPIError("⏳ Rate limited. Retrying in 60 seconds...")
            await asyncio.sleep(min(60, attempt * 5))
            continue

        if response.status_code >= 400:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise AnthropicAPIError(str(detail))

        return response.json()

    raise AnthropicAPIError("Unexpected API request failure")


async def fetch_paginated(
    endpoint: str,
    params: Dict[str, Any],
    use_cache: bool = True,
) -> List[Dict[str, Any]]:
    cache_key = build_cache_key(endpoint, params)

    if use_cache:
        cached = get_cached(cache_key)
        if cached is not None:
            return cached

    results: List[Dict[str, Any]] = []
    next_page: Optional[str] = None

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        while True:
            page_params = dict(params)
            if next_page:
                page_params["page"] = next_page

            payload = await _request_json(client, endpoint, page_params)
            results.extend(payload.get("data", []))

            if not payload.get("has_more"):
                break

            next_page = payload.get("next_page")
            if not next_page:
                break

    if use_cache:
        set_cache(cache_key, results)

    return results


def _iso_range(start: str, end: str) -> Dict[str, str]:
    return {
        "starting_at": f"{start}T00:00:00Z",
        "ending_at": f"{end}T23:59:59Z",
    }


async def get_usage_report(
    start: str,
    end: str,
    group_by: Optional[List[str]] = None,
    models: Optional[List[str]] = None,
    workspace_ids: Optional[List[str]] = None,
    bucket_width: str = "1d",
    limit: int = 31,
    use_cache: bool = True,
) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {
        **_iso_range(start, end),
        "bucket_width": bucket_width,
        "limit": limit,
    }

    if group_by:
        params["group_by[]"] = group_by
    if models:
        params["models[]"] = models
    if workspace_ids:
        params["workspace_ids[]"] = workspace_ids

    return await fetch_paginated(
        "organizations/usage_report/messages",
        params=params,
        use_cache=use_cache,
    )


async def get_cost_report(
    start: str,
    end: str,
    group_by: Optional[List[str]] = None,
    bucket_width: str = "1d",
    limit: int = 31,
    use_cache: bool = True,
) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {
        **_iso_range(start, end),
        "bucket_width": bucket_width,
        "limit": limit,
    }

    if group_by:
        params["group_by[]"] = group_by

    return await fetch_paginated(
        "organizations/cost_report",
        params=params,
        use_cache=use_cache,
    )


def _daterange(start_date: date, end_date: date) -> Iterable[date]:
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


async def get_claude_code_analytics(start: str, end: str, use_cache: bool = True) -> List[Dict[str, Any]]:
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)

    all_rows: List[Dict[str, Any]] = []

    for day in _daterange(start_date, end_date):
        params = {
            "starting_at": day.isoformat(),
            "limit": 1000,
        }
        rows = await fetch_paginated(
            "organizations/usage_report/claude_code",
            params=params,
            use_cache=use_cache,
        )
        all_rows.extend(rows)

    return all_rows
