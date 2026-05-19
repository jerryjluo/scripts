#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "plotly>=5.20",
#     "pandas>=2.2",
# ]
# ///
"""Plot ccusage daily/weekly/monthly costs plus a cumulative line chart."""

from __future__ import annotations

import json
import subprocess
import sys
import webbrowser
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


def fetch_usage() -> list[dict]:
    result = subprocess.run(
        ["npx", "ccusage", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    return data.get("daily", [])


def build_frame(daily: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(daily)
    if df.empty:
        sys.exit("ccusage returned no daily data")
    df["date"] = pd.to_datetime(df["period"])
    df = df.groupby("date", as_index=False)["totalCost"].sum().sort_values("date")
    full = pd.date_range(df["date"].min(), df["date"].max(), freq="D")
    df = df.set_index("date").reindex(full, fill_value=0.0).rename_axis("date").reset_index()
    return df


def main() -> None:
    daily = build_frame(fetch_usage())

    weekly = (
        daily.set_index("date")
        .resample("W-MON", label="left", closed="left")["totalCost"]
        .sum()
        .reset_index()
    )
    monthly = (
        daily.set_index("date")
        .resample("MS")["totalCost"]
        .sum()
        .reset_index()
    )
    cumulative = daily.assign(cumulative=daily["totalCost"].cumsum())

    fig = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=False,
        vertical_spacing=0.08,
        subplot_titles=(
            "Daily cost",
            "Weekly cost (week starting Monday)",
            "Monthly cost",
            "Cumulative cost",
        ),
    )

    fig.add_trace(
        go.Bar(
            x=daily["date"],
            y=daily["totalCost"],
            name="Daily",
            marker_color="#4C9AFF",
            hovertemplate="%{x|%Y-%m-%d}<br>$%{y:.2f}<extra></extra>",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Bar(
            x=weekly["date"],
            y=weekly["totalCost"],
            name="Weekly",
            marker_color="#36B37E",
            hovertemplate="week of %{x|%Y-%m-%d}<br>$%{y:.2f}<extra></extra>",
        ),
        row=2,
        col=1,
    )
    fig.add_trace(
        go.Bar(
            x=monthly["date"],
            y=monthly["totalCost"],
            name="Monthly",
            marker_color="#FF8B00",
            hovertemplate="%{x|%Y-%m}<br>$%{y:.2f}<extra></extra>",
        ),
        row=3,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=cumulative["date"],
            y=cumulative["cumulative"],
            name="Cumulative",
            mode="lines",
            line=dict(color="#6554C0", width=2),
            fill="tozeroy",
            fillcolor="rgba(101,84,192,0.15)",
            hovertemplate="%{x|%Y-%m-%d}<br>$%{y:.2f}<extra></extra>",
        ),
        row=4,
        col=1,
    )

    total = cumulative["cumulative"].iloc[-1]
    fig.update_layout(
        title=f"ccusage spend — total ${total:,.2f}",
        height=1100,
        showlegend=False,
        template="plotly_white",
        bargap=0.15,
    )
    for row in (1, 2, 3, 4):
        fig.update_yaxes(title_text="USD", tickprefix="$", row=row, col=1)

    out = Path("/tmp/ccusage-chart.html")
    fig.write_html(out, include_plotlyjs="cdn")
    print(f"wrote {out}")

    chrome_paths = [
        "open -a 'Google Chrome' {url}",
    ]
    url = out.as_uri()
    for tmpl in chrome_paths:
        if subprocess.run(tmpl.format(url=url), shell=True).returncode == 0:
            return
    webbrowser.open(url)


if __name__ == "__main__":
    main()
