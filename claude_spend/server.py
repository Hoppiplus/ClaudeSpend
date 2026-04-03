from __future__ import annotations

import calendar
import csv
import io
import os
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv, set_key
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response

from .api import AnthropicAPIError, get_claude_code_analytics, get_cost_report, get_usage_report
from .db import clear_cache, init_db
from .pricing import calc_cache_savings, calc_cost

load_dotenv()

app = FastAPI(title="claude-spend")
init_db()

STATIC_DIR = Path(__file__).parent / "static"
DASHBOARD_PATH = STATIC_DIR / "dashboard.html"
ENV_PATH = Path.cwd() / ".env"


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _default_range() -> Tuple[str, str]:
    today = _today_utc()
    start = today.replace(day=1)
    return start.isoformat(), today.isoformat()


def _resolve_range(start: str | None, end: str | None) -> Tuple[str, str]:
    if not start or not end:
        return _default_range()

    try:
        start_d = _parse_date(start)
        end_d = _parse_date(end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.") from exc

    if start_d > end_d:
        raise HTTPException(status_code=400, detail="Start date cannot be after end date.")

    return start_d.isoformat(), end_d.isoformat()


def _safe_int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _cost_cents_to_usd(value: Any) -> float:
    if value is None:
        return 0.0
    return round(int(value) / 100.0, 6)


def _mask_api_key(key: str | None) -> str | None:
    if not key:
        return None
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:10]}{'*' * max(0, len(key) - 14)}{key[-4:]}"


async def _load_usage_and_cost(start: str, end: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    try:
        usage_rows = await get_usage_report(start, end, group_by=["model"])
        cost_rows = await get_cost_report(start, end, group_by=["description"])
        return usage_rows, cost_rows
    except AnthropicAPIError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected server error: {exc}") from exc


async def _load_workspace_costs(start: str, end: str) -> List[Dict[str, Any]]:
    try:
        return await get_cost_report(start, end, group_by=["workspace_id"])
    except AnthropicAPIError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected server error: {exc}") from exc


def _aggregate_summary(
    usage_rows: List[Dict[str, Any]],
    cost_rows: List[Dict[str, Any]],
    start: str,
    end: str,
) -> Dict[str, Any]:
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_write = 0
    total_cache_savings = 0.0
    estimated_total_cost = 0.0

    for row in usage_rows:
        model = row.get("model") or "unknown"
        input_tokens = _safe_int(row.get("input_tokens"))
        output_tokens = _safe_int(row.get("output_tokens"))
        cache_read = _safe_int(row.get("cache_read_input_tokens"))
        cache_write = _safe_int(row.get("cache_creation_input_tokens"))

        total_input += input_tokens
        total_output += output_tokens
        total_cache_read += cache_read
        total_cache_write += cache_write

        estimated_total_cost += calc_cost(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )
        total_cache_savings += calc_cache_savings(model, cache_read)

    reported_total_cost = sum(_cost_cents_to_usd(row.get("cost")) for row in cost_rows)
    total_cost = reported_total_cost if reported_total_cost > 0 else round(estimated_total_cost, 6)

    start_d = _parse_date(start)
    end_d = _parse_date(end)
    days_elapsed = max(1, (end_d - start_d).days + 1)
    burn_rate_daily = total_cost / days_elapsed

    days_in_month = calendar.monthrange(end_d.year, end_d.month)[1]
    projected_month_total = burn_rate_daily * days_in_month

    fresh_input = max(0, total_input - total_cache_read)
    total_tokens = total_input + total_output + total_cache_read + total_cache_write

    cache_efficiency = 0.0
    if (fresh_input + total_cache_read) > 0:
        cache_efficiency = (total_cache_read / (fresh_input + total_cache_read)) * 100.0

    return {
        "start": start,
        "end": end,
        "total_cost_usd": round(total_cost, 2),
        "total_tokens": total_tokens,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cache_read_tokens": total_cache_read,
        "cache_write_tokens": total_cache_write,
        "burn_rate_daily_usd": round(burn_rate_daily, 2),
        "projected_month_total_usd": round(projected_month_total, 2),
        "cache_efficiency_percent": round(cache_efficiency, 2),
        "cache_savings_usd": round(total_cache_savings, 2),
    }


def _aggregate_spend_by_model(
    usage_rows: List[Dict[str, Any]],
    cost_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    items: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "model": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "estimated_cost_usd": 0.0,
            "reported_cost_usd": 0.0,
        }
    )

    for row in usage_rows:
        model = row.get("model") or "unknown"
        input_tokens = _safe_int(row.get("input_tokens"))
        output_tokens = _safe_int(row.get("output_tokens"))
        cache_read = _safe_int(row.get("cache_read_input_tokens"))
        cache_write = _safe_int(row.get("cache_creation_input_tokens"))

        entry = items[model]
        entry["model"] = model
        entry["input_tokens"] += input_tokens
        entry["output_tokens"] += output_tokens
        entry["cache_read_tokens"] += cache_read
        entry["cache_write_tokens"] += cache_write
        entry["estimated_cost_usd"] += calc_cost(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )

    for row in cost_rows:
        model = row.get("description") or "unknown"
        items[model]["model"] = model
        items[model]["reported_cost_usd"] += _cost_cents_to_usd(row.get("cost"))

    result: List[Dict[str, Any]] = []
    for entry in items.values():
        total_cost = entry["reported_cost_usd"] if entry["reported_cost_usd"] > 0 else entry["estimated_cost_usd"]
        result.append(
            {
                "model": entry["model"],
                "input_tokens": entry["input_tokens"],
                "output_tokens": entry["output_tokens"],
                "cache_read_tokens": entry["cache_read_tokens"],
                "cache_write_tokens": entry["cache_write_tokens"],
                "total_cost_usd": round(total_cost, 6),
            }
        )

    result.sort(key=lambda x: x["total_cost_usd"], reverse=True)
    return result


def _aggregate_daily_trend(
    usage_rows: List[Dict[str, Any]],
    cost_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    index: Dict[Tuple[str, str], Dict[str, Any]] = defaultdict(
        lambda: {
            "date": "",
            "model": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "estimated_cost_usd": 0.0,
            "reported_cost_usd": 0.0,
        }
    )

    for row in usage_rows:
        day = (row.get("start_time") or "")[:10]
        model = row.get("model") or "unknown"
        key = (day, model)

        input_tokens = _safe_int(row.get("input_tokens"))
        output_tokens = _safe_int(row.get("output_tokens"))
        cache_read = _safe_int(row.get("cache_read_input_tokens"))
        cache_write = _safe_int(row.get("cache_creation_input_tokens"))

        entry = index[key]
        entry["date"] = day
        entry["model"] = model
        entry["input_tokens"] += input_tokens
        entry["output_tokens"] += output_tokens
        entry["cache_read_tokens"] += cache_read
        entry["cache_write_tokens"] += cache_write
        entry["estimated_cost_usd"] += calc_cost(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )

    for row in cost_rows:
        day = (row.get("start_time") or "")[:10]
        model = row.get("description") or "unknown"
        key = (day, model)
        index[key]["date"] = day
        index[key]["model"] = model
        index[key]["reported_cost_usd"] += _cost_cents_to_usd(row.get("cost"))

    result: List[Dict[str, Any]] = []
    for entry in index.values():
        total_cost = entry["reported_cost_usd"] if entry["reported_cost_usd"] > 0 else entry["estimated_cost_usd"]
        result.append(
            {
                "date": entry["date"],
                "model": entry["model"],
                "input_tokens": entry["input_tokens"],
                "output_tokens": entry["output_tokens"],
                "cache_read_tokens": entry["cache_read_tokens"],
                "cache_write_tokens": entry["cache_write_tokens"],
                "total_cost_usd": round(total_cost, 6),
            }
        )

    result.sort(key=lambda x: (x["date"], x["model"]))
    return result


def _aggregate_workspaces(cost_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: Dict[str, float] = defaultdict(float)

    for row in cost_rows:
        workspace_id = row.get("workspace_id") or "default"
        items[workspace_id] += _cost_cents_to_usd(row.get("cost"))

    result = [{"workspace_id": key, "total_cost_usd": round(value, 6)} for key, value in items.items()]
    result.sort(key=lambda x: x["total_cost_usd"], reverse=True)
    return result


def _aggregate_claude_code(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "email": "",
            "sessions": 0,
            "lines_added": 0,
            "lines_removed": 0,
            "commits": 0,
            "pull_requests": 0,
            "total_cost_usd": 0.0,
            "accepted_actions": 0,
            "rejected_actions": 0,
        }
    )

    for row in rows:
        actor = row.get("actor", {})
        email = actor.get("email_address") or "unknown"

        entry = items[email]
        entry["email"] = email

        core_metrics = row.get("core_metrics", {})
        lines = core_metrics.get("lines_of_code", {})

        entry["sessions"] += _safe_int(core_metrics.get("num_sessions"))
        entry["lines_added"] += _safe_int(lines.get("added"))
        entry["lines_removed"] += _safe_int(lines.get("removed"))
        entry["commits"] += _safe_int(core_metrics.get("commits_by_claude_code"))
        entry["pull_requests"] += _safe_int(core_metrics.get("pull_requests_by_claude_code"))

        for model_entry in row.get("model_breakdown", []):
            estimated_cost = model_entry.get("estimated_cost", {})
            entry["total_cost_usd"] += _cost_cents_to_usd(estimated_cost.get("amount"))

        for tool_stats in row.get("tool_actions", {}).values():
            entry["accepted_actions"] += _safe_int(tool_stats.get("accepted"))
            entry["rejected_actions"] += _safe_int(tool_stats.get("rejected"))

    result: List[Dict[str, Any]] = []
    for entry in items.values():
        total_actions = entry["accepted_actions"] + entry["rejected_actions"]
        acceptance_rate = ((entry["accepted_actions"] / total_actions) * 100.0) if total_actions > 0 else 0.0

        result.append(
            {
                "email": entry["email"],
                "sessions": entry["sessions"],
                "lines_added": entry["lines_added"],
                "lines_removed": entry["lines_removed"],
                "commits": entry["commits"],
                "pull_requests": entry["pull_requests"],
                "total_cost_usd": round(entry["total_cost_usd"], 2),
                "acceptance_rate_percent": round(acceptance_rate, 2),
            }
        )

    result.sort(key=lambda x: x["total_cost_usd"], reverse=True)
    return result


@app.get("/", response_class=FileResponse)
async def root() -> FileResponse:
    return FileResponse(DASHBOARD_PATH)


@app.get("/api/summary")
async def summary(start: str | None = Query(default=None), end: str | None = Query(default=None)) -> Dict[str, Any]:
    start, end = _resolve_range(start, end)
    usage_rows, cost_rows = await _load_usage_and_cost(start, end)
    return _aggregate_summary(usage_rows, cost_rows, start, end)


@app.get("/api/spend-by-model")
async def spend_by_model(
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> Dict[str, Any]:
    start, end = _resolve_range(start, end)
    usage_rows, cost_rows = await _load_usage_and_cost(start, end)
    return {"items": _aggregate_spend_by_model(usage_rows, cost_rows)}


@app.get("/api/daily-trend")
async def daily_trend(
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> Dict[str, Any]:
    start, end = _resolve_range(start, end)
    usage_rows, cost_rows = await _load_usage_and_cost(start, end)
    return {"items": _aggregate_daily_trend(usage_rows, cost_rows)}


@app.get("/api/workspaces")
async def workspaces(
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> Dict[str, Any]:
    start, end = _resolve_range(start, end)
    workspace_cost_rows = await _load_workspace_costs(start, end)
    return {"items": _aggregate_workspaces(workspace_cost_rows)}


@app.get("/api/claude-code")
async def claude_code(
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> Dict[str, Any]:
    start, end = _resolve_range(start, end)
    try:
        rows = await get_claude_code_analytics(start, end)
    except AnthropicAPIError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected server error: {exc}") from exc

    return {"items": _aggregate_claude_code(rows)}


@app.get("/api/export/json")
async def export_json(
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> JSONResponse:
    start, end = _resolve_range(start, end)
    usage_rows, cost_rows = await _load_usage_and_cost(start, end)
    workspace_cost_rows = await _load_workspace_costs(start, end)

    payload = {
        "summary": _aggregate_summary(usage_rows, cost_rows, start, end),
        "spend_by_model": _aggregate_spend_by_model(usage_rows, cost_rows),
        "daily_trend": _aggregate_daily_trend(usage_rows, cost_rows),
        "workspaces": _aggregate_workspaces(workspace_cost_rows),
    }
    return JSONResponse(content=payload)


@app.get("/api/export/csv")
async def export_csv(
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> Response:
    start, end = _resolve_range(start, end)
    usage_rows, cost_rows = await _load_usage_and_cost(start, end)
    items = _aggregate_spend_by_model(usage_rows, cost_rows)

    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "model",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "total_cost_usd",
        ],
    )
    writer.writeheader()
    writer.writerows(items)

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="claude_spend_report.csv"'},
    )


@app.post("/api/refresh")
async def refresh() -> Dict[str, Any]:
    clear_cache()
    return {"ok": True, "message": "Cache cleared. Data will be re-fetched on next request."}


@app.get("/api/settings")
async def get_settings() -> Dict[str, Any]:
    key = os.getenv("ANTHROPIC_ADMIN_API_KEY")
    return {
        "has_api_key": bool(key),
        "masked_api_key": _mask_api_key(key),
        "env_path": str(ENV_PATH),
    }


@app.post("/api/settings")
async def save_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    api_key = (payload.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="API key is required.")
    if not api_key.startswith("sk-ant-admin"):
        raise HTTPException(
            status_code=400,
            detail="❌ This requires an Admin API key (sk-ant-admin...). Get one at console.anthropic.com → Settings → Admin Keys",
        )

    if not ENV_PATH.exists():
        ENV_PATH.write_text("", encoding="utf-8")

    set_key(str(ENV_PATH), "ANTHROPIC_ADMIN_API_KEY", api_key)
    os.environ["ANTHROPIC_ADMIN_API_KEY"] = api_key
    clear_cache()

    return {"ok": True, "message": "API key saved successfully."}
