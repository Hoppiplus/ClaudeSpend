from __future__ import annotations

import asyncio
import csv
import json
import os
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click
import uvicorn
from dotenv import load_dotenv, set_key
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .api import AnthropicAPIError, get_cost_report, get_usage_report
from .db import clear_cache, init_db
from .pricing import calc_cache_savings, calc_cost

console = Console()
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7842
ENV_PATH = Path.cwd() / ".env"


def _ensure_env_file() -> None:
    if not ENV_PATH.exists():
        ENV_PATH.write_text("", encoding="utf-8")


def _mask_api_key(key: str | None) -> str:
    if not key:
        return "Not configured"
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:10]}{'*' * max(0, len(key) - 14)}{key[-4:]}"


def _load_api_key() -> Optional[str]:
    load_dotenv(override=True)
    return os.getenv("ANTHROPIC_ADMIN_API_KEY")


def _validate_admin_key(api_key: str) -> None:
    if not api_key.startswith("sk-ant-admin"):
        raise click.ClickException(
            "This requires an Admin API key (sk-ant-admin...). Get one from console.anthropic.com → Settings → Admin Keys."
        )


def _save_api_key(api_key: str) -> None:
    _ensure_env_file()
    set_key(str(ENV_PATH), "ANTHROPIC_ADMIN_API_KEY", api_key)
    os.environ["ANTHROPIC_ADMIN_API_KEY"] = api_key
    clear_cache()


def _prompt_for_api_key_if_missing() -> None:
    api_key = _load_api_key()
    if api_key:
        return

    console.print(
        Panel.fit(
            "[bold yellow]claude-spend setup[/bold yellow]\n\n"
            "No Admin API key found in your local .env file.\n"
            "Enter your Anthropic Admin API key to continue.",
            border_style="yellow",
        )
    )
    entered = click.prompt("Admin API key", hide_input=True).strip()
    _validate_admin_key(entered)
    _save_api_key(entered)
    console.print("[green]✓ API key saved to local .env[/green]")


def _parse_cost_rows(cost_rows: list[dict]) -> float:
    return round(sum(int(row.get("cost", 0) or 0) / 100.0 for row in cost_rows), 2)


def _build_summary(usage_rows: list[dict], cost_rows: list[dict]) -> dict:
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_write = 0
    cache_savings = 0.0
    estimated_cost = 0.0

    for row in usage_rows:
        model = row.get("model") or row.get("description") or "unknown"
        input_tokens = int(row.get("input_tokens", 0) or 0)
        output_tokens = int(row.get("output_tokens", 0) or 0)
        cache_read = int(row.get("cache_read_input_tokens", 0) or 0)
        cache_write = int(row.get("cache_creation_input_tokens", 0) or 0)

        total_input += input_tokens
        total_output += output_tokens
        total_cache_read += cache_read
        total_cache_write += cache_write

        estimated_cost += calc_cost(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )
        cache_savings += calc_cache_savings(model, cache_read)

    total_cost = _parse_cost_rows(cost_rows)
    if total_cost <= 0:
        total_cost = round(estimated_cost, 2)

    total_tokens = total_input + total_output + total_cache_read + total_cache_write
    fresh_input = max(0, total_input - total_cache_read)
    cache_efficiency = (
        (total_cache_read / (fresh_input + total_cache_read)) * 100 if (fresh_input + total_cache_read) > 0 else 0.0
    )

    return {
        "total_cost_usd": round(total_cost, 2),
        "total_tokens": total_tokens,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cache_read_tokens": total_cache_read,
        "cache_write_tokens": total_cache_write,
        "cache_efficiency_percent": round(cache_efficiency, 2),
        "cache_savings_usd": round(cache_savings, 2),
    }


async def _fetch_summary_async(start: str, end: str) -> dict:
    usage_rows = await get_usage_report(start, end, group_by=["model"])
    cost_rows = await get_cost_report(start, end, group_by=["description"])
    return _build_summary(usage_rows, cost_rows)


async def _export_async(fmt: str, output: str, start: str, end: str) -> None:
    usage_rows = await get_usage_report(start, end, group_by=["model"])
    cost_rows = await get_cost_report(start, end, group_by=["description"])
    summary = _build_summary(usage_rows, cost_rows)
    out_path = Path(output)

    if fmt == "json":
        payload = {
            "start": start,
            "end": end,
            "summary": summary,
            "usage": usage_rows,
            "cost": cost_rows,
        }
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return

    if fmt == "csv":
        with out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "start_time",
                    "end_time",
                    "model",
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                ]
            )
            for row in usage_rows:
                writer.writerow(
                    [
                        row.get("start_time", ""),
                        row.get("end_time", ""),
                        row.get("model", ""),
                        row.get("input_tokens", 0),
                        row.get("output_tokens", 0),
                        row.get("cache_read_input_tokens", 0),
                        row.get("cache_creation_input_tokens", 0),
                    ]
                )
        return

    raise click.ClickException(f"Unsupported export format: {fmt}")


def _print_summary_table(summary: dict, start: str, end: str) -> None:
    table = Table(title=f"claude-spend summary ({start} to {end})")
    table.add_column("Metric", style="cyan", no_wrap=True)
    table.add_column("Value", style="white")

    table.add_row("Total cost", f"${summary['total_cost_usd']:.2f}")
    table.add_row("Total tokens", f"{summary['total_tokens']:,}")
    table.add_row("Input tokens", f"{summary['input_tokens']:,}")
    table.add_row("Output tokens", f"{summary['output_tokens']:,}")
    table.add_row("Cache read tokens", f"{summary['cache_read_tokens']:,}")
    table.add_row("Cache write tokens", f"{summary['cache_write_tokens']:,}")
    table.add_row("Cache efficiency", f"{summary['cache_efficiency_percent']:.2f}%")
    table.add_row("Cache savings", f"${summary['cache_savings_usd']:.2f}")

    console.print(table)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--host", default=DEFAULT_HOST, show_default=True, help="Host to bind the local server.")
@click.option("--port", default=DEFAULT_PORT, show_default=True, type=int, help="Port to bind the local server.")
@click.option("--summary", "show_summary", is_flag=True, help="Fetch and print a spend summary without launching the dashboard.")
@click.option("--export", "export_format", type=click.Choice(["csv", "json"]), help="Export the current report without launching the dashboard.")
@click.option("--output", type=str, help="Output file path for export.")
@click.option("--set-key", type=str, help="Save an Anthropic Admin API key to the local .env file.")
@click.option("--start", type=str, default=None, help="Start date in YYYY-MM-DD format.")
@click.option("--end", type=str, default=None, help="End date in YYYY-MM-DD format.")
@click.option("--no-browser", is_flag=True, help="Start the local dashboard without opening a browser tab.")
def main(
    host: str,
    port: int,
    show_summary: bool,
    export_format: str | None,
    output: str | None,
    set_key: str | None,
    start: str | None,
    end: str | None,
    no_browser: bool,
) -> None:
    init_db()
    load_dotenv(override=True)

    if set_key:
        set_key = set_key.strip()
        _validate_admin_key(set_key)
        _save_api_key(set_key)
        console.print(f"[green]✓ Saved Admin API key:[/green] {_mask_api_key(set_key)}")
        return

    if show_summary or export_format:
        _prompt_for_api_key_if_missing()

    today = datetime.now(timezone.utc).date()
    if not start:
        start = today.replace(day=1).isoformat()
    if not end:
        end = today.isoformat()

    if show_summary:
        try:
            summary = asyncio.run(_fetch_summary_async(start, end))
            _print_summary_table(summary, start, end)
            return
        except AnthropicAPIError as exc:
            raise click.ClickException(str(exc)) from exc
        except Exception as exc:
            raise click.ClickException(f"Failed to fetch summary: {exc}") from exc

    if export_format:
        if not output:
            output = str(Path.cwd() / f"claude_spend_report.{export_format}")
        try:
            asyncio.run(_export_async(export_format, output, start, end))
            console.print(f"[green]✓ Exported report to[/green] {output}")
            return
        except AnthropicAPIError as exc:
            raise click.ClickException(str(exc)) from exc
        except Exception as exc:
            raise click.ClickException(f"Failed to export report: {exc}") from exc

    _prompt_for_api_key_if_missing()

    url = f"http://{host}:{port}"
    console.print(
        Panel.fit(
            f"[bold green]claude-spend is starting[/bold green]\n\n"
            f"Dashboard: [cyan]{url}[/cyan]\n"
            f"API key: [white]{_mask_api_key(_load_api_key())}[/white]",
            border_style="green",
        )
    )

    if not no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            console.print("[yellow]Could not open browser automatically. Open the URL manually.[/yellow]")

    uvicorn.run("claude_spend.server:app", host=host, port=port, reload=False)
