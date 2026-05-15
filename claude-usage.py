#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27",
#   "rich>=13",
# ]
# ///
"""Show current Claude Code Max plan usage by calling the same internal endpoint
that the in-CLI `/usage` dialog uses.

Endpoint: GET https://api.anthropic.com/api/oauth/usage
Auth:     Bearer token from the macOS keychain item "Claude Code-credentials".

This is an undocumented endpoint — it can change or break without notice.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

KEYCHAIN_SERVICE = "Claude Code-credentials"
DEFAULT_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"

console = Console()


def read_access_token() -> str:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    cmd = ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", user, "-w"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        sys.exit(
            f"Failed to read keychain item '{KEYCHAIN_SERVICE}'.\n"
            f"  stderr: {e.stderr.strip()}\n"
            "If macOS prompted you to allow access, run again and click Allow."
        )
    raw = result.stdout.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit("Keychain payload was not JSON — Claude Code may have changed its credential format.")
    token = data.get("claudeAiOauth", {}).get("accessToken")
    if not token:
        sys.exit("No claudeAiOauth.accessToken in keychain payload.")
    return token


def fetch_usage(token: str, endpoint: str) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-usage.py/1.0",
    }
    try:
        resp = httpx.get(endpoint, headers=headers, timeout=15.0)
    except httpx.HTTPError as e:
        sys.exit(f"Request failed: {e}")
    if resp.status_code != 200:
        sys.exit(f"HTTP {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def format_pct(pct: float | int | None) -> str:
    if pct is None:
        return "—"
    color = "green" if pct < 60 else "yellow" if pct < 85 else "red"
    return f"[{color}]{pct:.0f}%[/{color}]"


def format_bar(pct: float | int | None, width: int = 20) -> str:
    if pct is None:
        return ""
    filled = int(round((pct / 100) * width))
    filled = max(0, min(width, filled))
    color = "green" if pct < 60 else "yellow" if pct < 85 else "red"
    return f"[{color}]{'█' * filled}[/{color}][dim]{'░' * (width - filled)}[/dim]"


def format_resets(iso_str: str | None) -> str:
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return iso_str
    delta = dt - datetime.now().astimezone()
    secs = int(delta.total_seconds())
    if secs < 0:
        rel = "just reset"
    elif secs < 3600:
        rel = f"in {secs // 60}m"
    elif secs < 86400:
        rel = f"in {secs // 3600}h{(secs % 3600) // 60}m"
    else:
        rel = f"in {secs // 86400}d{(secs % 86400) // 3600}h"
    return f"{dt.strftime('%a %H:%M')} ({rel})"


def render(payload: dict) -> None:
    rows = [
        ("5-hour session", payload.get("five_hour")),
        ("7-day total", payload.get("seven_day")),
        ("7-day Opus", payload.get("seven_day_opus")),
        ("7-day Sonnet", payload.get("seven_day_sonnet")),
    ]

    table = Table(title="Claude Code usage", show_header=True, header_style="bold")
    table.add_column("Window")
    table.add_column("Util", justify="right")
    table.add_column("", no_wrap=True)
    table.add_column("Resets")

    any_row = False
    for label, block in rows:
        if not isinstance(block, dict):
            continue
        pct = block.get("utilization")
        if pct is None:
            continue
        table.add_row(label, format_pct(pct), format_bar(pct), format_resets(block.get("resets_at")))
        any_row = True

    if not any_row:
        console.print(Panel.fit("[yellow]Unrecognized response — printing raw JSON.[/yellow]"))
        console.print_json(data=payload)
        return

    console.print(table)

    extra = payload.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled"):
        used = extra.get("used_credits") or 0
        limit = extra.get("monthly_limit") or 0
        currency = extra.get("currency", "USD")
        console.print(
            f"[dim]Extra usage:[/dim] {used:g} / {limit:g} {currency} this month"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Claude Code Max plan usage.")
    parser.add_argument("--json", action="store_true", help="Print raw JSON response.")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Override usage endpoint URL.")
    args = parser.parse_args()

    token = read_access_token()
    payload = fetch_usage(token, args.endpoint)

    if args.json:
        console.print_json(data=payload)
    else:
        render(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
