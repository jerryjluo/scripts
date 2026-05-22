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

import argparse
import json
import os
import subprocess
import sys
import webbrowser
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

os.environ["PATH"] = os.pathsep.join([
    "/Users/jerryluo/.local/bin",
    "/Users/jerryluo/.local/share/mise/shims",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    os.environ.get("PATH", ""),
])


PRESETS = (
    "all",
    "ytd",
    "this-year",
    "last-year",
    "this-month",
    "last-month",
    "this-week",
    "last-week",
    "last-7d",
    "last-30d",
    "last-90d",
)


def preset_range(name: str, today: date) -> tuple[date | None, date | None]:
    if name == "all":
        return None, None
    if name in ("ytd", "this-year"):
        return date(today.year, 1, 1), today
    if name == "last-year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if name == "this-month":
        return date(today.year, today.month, 1), today
    if name == "last-month":
        first_this = date(today.year, today.month, 1)
        last_prev = first_this - timedelta(days=1)
        return date(last_prev.year, last_prev.month, 1), last_prev
    if name == "this-week":
        return today - timedelta(days=today.weekday()), today
    if name == "last-week":
        this_mon = today - timedelta(days=today.weekday())
        return this_mon - timedelta(days=7), this_mon - timedelta(days=1)
    if name == "last-7d":
        return today - timedelta(days=6), today
    if name == "last-30d":
        return today - timedelta(days=29), today
    if name == "last-90d":
        return today - timedelta(days=89), today
    raise ValueError(f"unknown preset: {name}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "preset",
        nargs="?",
        default="all",
        choices=PRESETS,
        help="initially selected preset in the dropdown (default: all)",
    )
    p.add_argument("--start", type=date.fromisoformat, help="custom start date YYYY-MM-DD (adds a 'custom' option)")
    p.add_argument("--end", type=date.fromisoformat, help="custom end date YYYY-MM-DD (adds a 'custom' option)")
    p.add_argument("--no-open", action="store_true", help="don't open the chart in a browser")
    p.add_argument("--out", type=Path, default=Path("/tmp/ccusage-chart.html"), help="output HTML path")
    return p.parse_args(argv)


def fetch_usage() -> list[dict]:
    result = subprocess.run(
        ["npx", "ccusage", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    return data.get("daily", [])


def build_daily(raw: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(raw)
    if df.empty:
        sys.exit("ccusage returned no daily data")
    df["date"] = pd.to_datetime(df["period"])
    return df.groupby("date", as_index=False)["totalCost"].sum().sort_values("date")


def slice_range(
    daily: pd.DataFrame,
    start: date | None,
    end: date | None,
) -> pd.DataFrame:
    lo = pd.Timestamp(start) if start else daily["date"].min()
    hi = pd.Timestamp(end) if end else daily["date"].max()
    if lo > hi:
        return pd.DataFrame({"date": pd.to_datetime([]), "totalCost": []})
    masked = daily[(daily["date"] >= lo) & (daily["date"] <= hi)]
    full = pd.date_range(lo, hi, freq="D")
    return (
        masked.set_index("date")
        .reindex(full, fill_value=0.0)
        .rename_axis("date")
        .reset_index()
    )


def aggregates(daily: pd.DataFrame) -> dict[str, pd.DataFrame]:
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
    return {"daily": daily, "weekly": weekly, "monthly": monthly, "cumulative": cumulative}


def range_label(start: date | None, end: date | None) -> str:
    if start and end:
        return f"{start} → {end}"
    if start:
        return f"{start} → today"
    if end:
        return f"start → {end}"
    return "all time"


def main() -> None:
    args = parse_args(sys.argv[1:])
    today = date.today()
    full_daily = build_daily(fetch_usage())

    views: list[tuple[str, date | None, date | None, dict[str, pd.DataFrame]]] = []
    if args.start or args.end:
        views.append(("custom", args.start, args.end, aggregates(slice_range(full_daily, args.start, args.end))))
    for name in PRESETS:
        s, e = preset_range(name, today)
        views.append((name, s, e, aggregates(slice_range(full_daily, s, e))))

    default_label = "custom" if (args.start or args.end) else args.preset
    default_idx = next(i for i, v in enumerate(views) if v[0] == default_label)

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

    traces_per_view = 4
    for view_idx, (_, _, _, agg) in enumerate(views):
        visible = view_idx == default_idx
        fig.add_trace(
            go.Bar(
                x=agg["daily"]["date"],
                y=agg["daily"]["totalCost"],
                name="Daily",
                marker_color="#4C9AFF",
                visible=visible,
                hovertemplate="%{x|%Y-%m-%d}<br>$%{y:.2f}<extra></extra>",
            ),
            row=1, col=1,
        )
        fig.add_trace(
            go.Bar(
                x=agg["weekly"]["date"],
                y=agg["weekly"]["totalCost"],
                name="Weekly",
                marker_color="#36B37E",
                visible=visible,
                hovertemplate="week of %{x|%Y-%m-%d}<br>$%{y:.2f}<extra></extra>",
            ),
            row=2, col=1,
        )
        fig.add_trace(
            go.Bar(
                x=agg["monthly"]["date"],
                y=agg["monthly"]["totalCost"],
                name="Monthly",
                marker_color="#FF8B00",
                visible=visible,
                hovertemplate="%{x|%Y-%m}<br>$%{y:.2f}<extra></extra>",
            ),
            row=3, col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=agg["cumulative"]["date"],
                y=agg["cumulative"]["cumulative"],
                name="Cumulative",
                mode="lines",
                line=dict(color="#6554C0", width=2),
                fill="tozeroy",
                fillcolor="rgba(101,84,192,0.15)",
                visible=visible,
                hovertemplate="%{x|%Y-%m-%d}<br>$%{y:.2f}<extra></extra>",
            ),
            row=4, col=1,
        )

    def title_for(label: str, s: date | None, e: date | None, agg: dict[str, pd.DataFrame]) -> str:
        total = agg["cumulative"]["cumulative"].iloc[-1] if len(agg["cumulative"]) else 0.0
        return f"ccusage spend — {label} ({range_label(s, e)}) — total ${total:,.2f}"

    buttons = []
    for view_idx, (label, s, e, agg) in enumerate(views):
        visible_flags = [False] * (len(views) * traces_per_view)
        for i in range(traces_per_view):
            visible_flags[view_idx * traces_per_view + i] = True
        buttons.append(dict(
            label=label,
            method="update",
            args=[
                {"visible": visible_flags},
                {"title.text": title_for(label, s, e, agg)},
            ],
        ))

    default_view = views[default_idx]
    fig.update_layout(
        title=title_for(default_view[0], default_view[1], default_view[2], default_view[3]),
        height=1100,
        showlegend=False,
        template="plotly_white",
        bargap=0.15,
        updatemenus=[dict(
            type="dropdown",
            buttons=buttons,
            active=default_idx,
            x=1.0,
            xanchor="right",
            y=1.06,
            yanchor="bottom",
            bgcolor="white",
            bordercolor="#cbd5e0",
            pad={"l": 8, "r": 8, "t": 4, "b": 4},
        )],
        annotations=list(fig.layout.annotations) + [dict(
            text="Range:",
            x=1.0, xref="paper", xanchor="right",
            y=1.10, yref="paper", yanchor="bottom",
            showarrow=False,
            xshift=-130,
            font=dict(size=12, color="#4a5568"),
        )],
    )
    for row in (1, 2, 3, 4):
        fig.update_yaxes(title_text="USD", tickprefix="$", row=row, col=1)

    out = args.out
    fig.write_html(out, include_plotlyjs="cdn")
    print(f"wrote {out}")

    if args.no_open:
        return
    url = out.resolve().as_uri()
    if subprocess.run(["open", "-a", "Google Chrome", url]).returncode != 0:
        webbrowser.open(url)


if __name__ == "__main__":
    main()
