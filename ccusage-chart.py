#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "plotly>=5.20",
#     "pandas>=2.2",
# ]
# ///
"""Plot ccusage daily/weekly/monthly costs plus a cumulative line chart.

Costs are split by agent (Claude Code vs Codex) and rendered as stacked bars.
"""

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


# Per-agent series. Each tuple: (column name, display label, hex color, the
# ccusage subcommand to fetch it from, and the cost field that subcommand uses).
AGENTS = (
    ("claude", "Claude Code", "#4C9AFF", "claude", "totalCost"),
    ("codex", "Codex", "#10A37F", "codex", "costUSD"),
)
AGENT_COLS = [a[0] for a in AGENTS]


def rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


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


def fetch_usage(subcommand: str) -> list[dict]:
    result = subprocess.run(
        ["npx", "ccusage", subcommand, "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    return data.get("daily", [])


def build_daily() -> pd.DataFrame:
    """One row per date with a cost column per agent (missing days -> 0)."""
    series = []
    for col, _label, _color, subcommand, cost_key in AGENTS:
        raw = fetch_usage(subcommand)
        if not raw:
            series.append(pd.DataFrame({"date": pd.to_datetime([]), col: pd.Series(dtype=float)}))
            continue
        df = pd.DataFrame(raw)
        df["date"] = pd.to_datetime(df["date"])
        agg = df.groupby("date", as_index=False)[cost_key].sum()
        series.append(agg.rename(columns={cost_key: col}))

    merged = series[0]
    for nxt in series[1:]:
        merged = merged.merge(nxt, on="date", how="outer")
    if merged.empty:
        sys.exit("ccusage returned no daily data")
    return merged[["date", *AGENT_COLS]].fillna(0.0).sort_values("date").reset_index(drop=True)


def slice_range(
    daily: pd.DataFrame,
    start: date | None,
    end: date | None,
) -> pd.DataFrame:
    lo = pd.Timestamp(start) if start else daily["date"].min()
    hi = pd.Timestamp(end) if end else daily["date"].max()
    if pd.isna(lo) or pd.isna(hi) or lo > hi:
        return pd.DataFrame({"date": pd.to_datetime([]), **{c: [] for c in AGENT_COLS}})
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
        .resample("W-MON", label="left", closed="left")[AGENT_COLS]
        .sum()
        .reset_index()
    )
    monthly = (
        daily.set_index("date")
        .resample("MS")[AGENT_COLS]
        .sum()
        .reset_index()
    )
    cumulative = daily.copy()
    for col in AGENT_COLS:
        cumulative[f"{col}_cum"] = cumulative[col].cumsum()
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
    full_daily = build_daily()

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

    # Per view we add: a stacked Bar pair (claude+codex) for daily/weekly/monthly,
    # plus a stacked-area Scatter pair for cumulative — two traces per agent per row.
    traces_per_view = 4 * len(AGENTS)
    bar_rows = (
        ("daily", "date", 1, "%{x|%Y-%m-%d}"),
        ("weekly", "date", 2, "week of %{x|%Y-%m-%d}"),
        ("monthly", "date", 3, "%{x|%Y-%m}"),
    )
    for view_idx, (_, _, _, agg) in enumerate(views):
        visible = view_idx == default_idx
        for key, xcol, row, xfmt in bar_rows:
            for col, label, color, _sub, _ck in AGENTS:
                fig.add_trace(
                    go.Bar(
                        x=agg[key][xcol],
                        y=agg[key][col],
                        name=label,
                        legendgroup=label,
                        showlegend=(row == 1),
                        marker_color=color,
                        visible=visible,
                        hovertemplate=f"{xfmt}<br>{label}: $%{{y:.2f}}<extra></extra>",
                    ),
                    row=row, col=1,
                )
        for col, label, color, _sub, _ck in AGENTS:
            fig.add_trace(
                go.Scatter(
                    x=agg["cumulative"]["date"],
                    y=agg["cumulative"][f"{col}_cum"],
                    name=label,
                    legendgroup=label,
                    showlegend=False,
                    mode="lines",
                    line=dict(color=color, width=1.5),
                    stackgroup=f"cum{view_idx}",
                    fillcolor=rgba(color, 0.30),
                    visible=visible,
                    hovertemplate=f"%{{x|%Y-%m-%d}}<br>{label}: $%{{y:.2f}}<extra></extra>",
                ),
                row=4, col=1,
            )

    def title_for(label: str, s: date | None, e: date | None, agg: dict[str, pd.DataFrame]) -> str:
        cum = agg["cumulative"]
        totals = {c: (cum[f"{c}_cum"].iloc[-1] if len(cum) else 0.0) for c in AGENT_COLS}
        grand = sum(totals.values())
        breakdown = " · ".join(
            f"{lbl} ${totals[c]:,.2f}" for c, lbl, *_ in AGENTS
        )
        return f"ccusage spend — {label} ({range_label(s, e)}) — total ${grand:,.2f} ({breakdown})"

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
        title=dict(
            text=title_for(default_view[0], default_view[1], default_view[2], default_view[3]),
            x=0.5, xanchor="center",
            y=0.985, yanchor="top",
            font=dict(size=16),
        ),
        height=1100,
        margin=dict(t=120, l=80, r=80, b=80),
        showlegend=True,
        barmode="stack",
        legend=dict(
            orientation="h",
            x=0.5, xanchor="center",
            y=1.05, yanchor="top",
        ),
        template="plotly_white",
        bargap=0.15,
        updatemenus=[dict(
            type="dropdown",
            buttons=buttons,
            active=default_idx,
            x=1.0,
            xanchor="right",
            y=1.05,
            yanchor="top",
            bgcolor="white",
            bordercolor="#cbd5e0",
            pad={"l": 8, "r": 8, "t": 4, "b": 4},
        )],
        annotations=list(fig.layout.annotations) + [dict(
            text="Range:",
            x=1.0, xref="paper", xanchor="right",
            y=1.045, yref="paper", yanchor="top",
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
