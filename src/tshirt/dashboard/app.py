"""Trendy TShirt Co — ad analysis & recommendation dashboard (Streamlit).

Launch with `tshirt-dash` or by double-clicking "Launch Dashboard.command".
Reads live from Supabase; the nightly job keeps the data fresh.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

# Absolute imports: Streamlit runs this file as a top-level script (__main__),
# so relative imports would fail. The `tshirt` package is installed (pip install -e .).
from tshirt.config import Config
from tshirt.db.connection import connect
from tshirt.jobs.recommend import ACTIONS, build_recommendations
from tshirt.dashboard import queries

st.set_page_config(page_title="Trendy TShirt Co — Ads", page_icon="👕", layout="wide")

ACTION_COLOR = {
    "SCALE": "#1a7f37",
    "HOLD": "#9a6700",
    "INVESTIGATE": "#bc4c00",
    "KILL": "#cf222e",
    "GATHER": "#6e7781",
    "OFF": "#8c8c8c",
}


@st.cache_data(ttl=300)
def _recommendations(since: str) -> pd.DataFrame:
    cfg = Config.load()
    with connect(cfg) as conn:
        recos = build_recommendations(conn, cfg, since)
    return pd.DataFrame([r.__dict__ for r in recos])


def _gate() -> Config:
    cfg = Config.load()
    missing = cfg.missing_required()
    if missing:
        st.title("👕 Trendy TShirt Co")
        st.error("Not ready to run yet. Fill these in `.env`:")
        st.code("\n".join(missing))
        st.info("Then run `tshirt-migrate` and `tshirt-nightly`, and reload this page.")
        st.stop()
    return cfg


def main() -> None:
    cfg = _gate()

    # --- sidebar: window control ---
    st.sidebar.title("👕 Trendy TShirt Co")
    today = date.today()
    preset = st.sidebar.selectbox(
        "Window",
        ["Last 7 days", "Last 14 days", "Last 30 days", "Last 60 days",
         "Last 90 days", "All data", "Custom range…"],
        index=3,
    )
    if preset == "Custom range…":
        rng = st.sidebar.date_input(
            "Date range", value=(today - timedelta(days=30), today), max_value=today,
        )
        if isinstance(rng, (list, tuple)) and len(rng) == 2:
            since, until = rng[0].isoformat(), rng[1].isoformat()
        else:  # user is mid-selection (only the start picked)
            start = rng[0] if isinstance(rng, (list, tuple)) else rng
            since, until = start.isoformat(), today.isoformat()
        window_label = f"{since} to {until}"
    elif preset == "All data":
        since, until, window_label = queries.earliest_order_date(), today.isoformat(), "all data"
    else:
        days = {"Last 7 days": 7, "Last 14 days": 14, "Last 30 days": 30,
                "Last 60 days": 60, "Last 90 days": 90}[preset]
        since, until, window_label = (today - timedelta(days=days)).isoformat(), today.isoformat(), f"last {days} days"
    if st.sidebar.button("🔄 Reload", use_container_width=True,
                         help="Re-read the latest synced data. Does NOT pull from Shopify/Meta."):
        st.cache_data.clear()
        st.rerun()
    if st.sidebar.button("⬇️ Pull latest data", use_container_width=True,
                         help="Fetch new orders/ads from Shopify, Printify & Meta now (a few minutes)."):
        with st.spinner("Pulling from Shopify, Printify & Meta... (a few minutes)"):
            proc = subprocess.run(
                [str(Path(sys.executable).parent / "tshirt-nightly")],
                capture_output=True, text=True,
            )
        if proc.returncode == 0:
            st.cache_data.clear()
            st.sidebar.success("Data updated.")
            st.rerun()
        else:
            st.sidebar.error("Pull failed:")
            st.sidebar.code((proc.stderr or proc.stdout or "")[-800:])
    sync = queries.last_sync()
    if sync:
        st.sidebar.caption(f"Last data pull: {sync['finished_at'] or sync['started_at']}")
    st.sidebar.caption("Set up the nightly auto-refresh so this stays current on its own (see README).")

    try:
        k = queries.kpis(since, until)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read the database: {exc}")
        st.info("Have you run `tshirt-migrate` and `tshirt-nightly` yet?")
        st.stop()

    st.title("Profit & ad performance")
    st.caption(f"Window: {window_label}")

    # --- HERO: true overall operating profit ---
    pnl = queries.operating_pnl(since, until)
    hero_left, hero_right = st.columns([1, 2])
    with hero_left:
        sign = "" if pnl["profit"] >= 0 else "-"
        color = "#1a7f37" if pnl["profit"] >= 0 else "#cf222e"
        margin = f"{pnl['margin_pct']}% margin" if pnl["margin_pct"] is not None else ""
        st.markdown(
            f"<div style='padding:4px 0'>"
            f"<div style='font-size:0.9rem;color:#6e7781'>Operating profit ({window_label})</div>"
            f"<div style='font-size:2.6rem;font-weight:800;color:{color};line-height:1.1'>"
            f"{sign}${abs(pnl['profit']):,.0f}</div>"
            f"<div style='color:{color};font-weight:600'>{margin}</div></div>",
            unsafe_allow_html=True,
        )
        st.caption(f"Net revenue (after discounts, ${pnl['refunds']:,.0f} refunds & tax) "
                   "− Printify (prod+ship+tax) − real payment fees − ALL ad spend"
                   + (" − fixed overhead." if pnl["fixed"] else "."))
    with hero_right:
        rows = [
            ("Net revenue (after discounts & refunds)", pnl["revenue"]),
            ("− Printify cost (prod + ship + tax)", -pnl["cogs"]),
            ("− Payment fees (real)", -pnl["fees"]),
            ("− Meta ad spend", -pnl["ad_spend"]),
        ]
        if pnl["fixed"]:
            rows.append(("− Fixed overhead (prorated)", -pnl["fixed"]))
        rows.append(("= Operating profit", pnl["profit"]))
        pl = pd.DataFrame({"": [r[0] for r in rows], "Amount": [r[1] for r in rows]})
        st.dataframe(
            pl.style.format({"Amount": "${:,.0f}"}),
            use_container_width=True, hide_index=True,
        )

    st.divider()

    # --- efficiency KPI row ---
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Ad spend", f"${k['ad_spend']:,.0f}")
    c2.metric("Shopify revenue", f"${k['shopify_revenue']:,.0f}")
    c3.metric("MER", k["mer"] if k["mer"] is not None else "—",
              help="Shopify revenue / ad spend (ground-truth blended efficiency)")
    c4.metric("Meta pixel ROAS", k["meta_roas"] if k["meta_roas"] is not None else "—",
              help="Meta's own attribution; tends to over-report")
    c5.metric("Orders", f"{k['orders']:,}")
    c6.metric("Shirts", k["shirts"])

    # --- profit over time ---
    st.subheader("Profit over time")
    vcol, ecol = st.columns([3, 1])
    view = vcol.radio(
        "view", ["Combined", "Daily", "7-day average", "Cumulative", "Spend vs revenue"],
        horizontal=True, label_visibility="collapsed",
    )
    show_events = ecol.checkbox("Ad launches", value=False, help="Mark when each ad set first spent")
    pnl_df = queries.daily_pnl(since, until)
    if pnl_df.empty:
        st.info("No daily data in this window yet.")
    else:
        pnl_df = pnl_df.copy()
        pnl_df["cum"] = pnl_df["profit"].cumsum().round(2)
        pnl_df["roll7"] = pnl_df["profit"].rolling(7, min_periods=1).mean().round(2)
        loss_floor = float(min(pnl_df["profit"].min(), 0))
        # shaded "losing money" band below zero
        band = alt.Chart(pd.DataFrame({"lo": [loss_floor], "hi": [0]})).mark_rect(
            color="#cf222e", opacity=0.05).encode(y="lo:Q", y2="hi:Q")
        zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
            color="#888", strokeDash=[4, 3]).encode(y="y:Q")
        day_tt = [alt.Tooltip("date:T", title="Day"), alt.Tooltip("profit:Q", title="Profit", format="$,.2f")]

        if view in ("Combined", "Daily"):
            bars = alt.Chart(pnl_df).mark_bar().encode(
                x=alt.X("date:T", title=None),
                y=alt.Y("profit:Q", title="Daily net profit ($)"),
                color=alt.condition("datum.profit >= 0", alt.value("#1a7f37"), alt.value("#cf222e")),
                tooltip=day_tt,
            )
            left = alt.layer(band, bars, zero)
            if view == "Combined":
                line = alt.Chart(pnl_df).mark_line(color="#185FA5", strokeWidth=2).encode(
                    x="date:T", y=alt.Y("cum:Q", title="Cumulative ($)"),
                    tooltip=[alt.Tooltip("cum:Q", title="Cumulative", format="$,.2f")])
                chart = alt.layer(left, line).resolve_scale(y="independent")
                cap = "Bars = daily profit (green) / loss (red). Blue line = cumulative profit (right axis)."
            else:
                chart = left
                cap = "Green bar = profitable day, red = loss day. Shaded band = losing territory."
        elif view == "7-day average":
            chart = alt.layer(band, zero, alt.Chart(pnl_df).mark_area(
                line={"color": "#1f6feb"}, color="rgba(31,111,235,0.12)").encode(
                x=alt.X("date:T", title=None),
                y=alt.Y("roll7:Q", title="7-day avg net profit ($)"),
                tooltip=[alt.Tooltip("date:T", title="Day"), alt.Tooltip("roll7:Q", title="7-day avg", format="$,.2f")]))
            cap = "7-day rolling average — smooths daily noise to show the trend."
        elif view == "Cumulative":
            chart = alt.layer(zero, alt.Chart(pnl_df).mark_area(
                line={"color": "#1a7f37"}, color="rgba(26,127,55,0.15)").encode(
                x=alt.X("date:T", title=None),
                y=alt.Y("cum:Q", title="Cumulative profit ($)"),
                tooltip=[alt.Tooltip("date:T", title="Day"), alt.Tooltip("cum:Q", title="Cumulative", format="$,.2f")]))
            cap = "Running total. Rising = making money; falling = losing money."
        else:  # Spend vs revenue
            cdf = pnl_df.melt("date", ["rev", "ad"], "series", "amount").replace(
                {"rev": "revenue", "ad": "ad spend"})
            chart = alt.Chart(cdf).mark_line().encode(
                x=alt.X("date:T", title=None), y=alt.Y("amount:Q", title="$"),
                color=alt.Color("series:N", title=None),
                tooltip=["date:T", "series:N", alt.Tooltip("amount:Q", format="$,.2f")])
            cap = "Daily ad spend vs net revenue."

        if show_events:
            ev = queries.adset_launches(since, until)
            if not ev.empty:
                ev = ev.copy()
                ev["launch"] = pd.to_datetime(ev["launch"])
                rules = alt.Chart(ev).mark_rule(color="#9a6700", strokeDash=[2, 2]).encode(
                    x="launch:T",
                    tooltip=[alt.Tooltip("name:N", title="Ad set launched"),
                             alt.Tooltip("launch:T", title="Date")])
                chart = chart + rules

        st.altair_chart(chart.properties(height=320), use_container_width=True)
        st.caption(cap + (" Amber dashed = ad-set launch." if show_events else ""))

    # --- action board ---
    ab_l, ab_r = st.columns([3, 1])
    ab_l.subheader("What to do (per ad set)")
    show_off = ab_r.checkbox("Show paused", value=False,
                             help="Include ad sets that are already off (OFF)")
    recos = _recommendations(since)
    if recos.empty:
        st.info("No ad-set data yet. Run `tshirt-nightly` after your Meta ads have spend.")
    else:
        counts = recos["action"].value_counts().to_dict()
        cols = st.columns(len(ACTIONS))
        for col, action in zip(cols, ACTIONS):
            col.markdown(
                f"<div style='text-align:center'><span style='color:{ACTION_COLOR[action]};"
                f"font-size:1.6rem;font-weight:700'>{counts.get(action, 0)}</span><br>"
                f"<small>{action}</small></div>",
                unsafe_allow_html=True,
            )
        st.divider()
        n_off = int((recos["action"] == "OFF").sum())
        board = recos if show_off else recos[recos["action"] != "OFF"]
        if board.empty:
            st.info("No live ad sets in this window. Toggle 'Show paused' to see off ad sets.")
        else:
            show_cols = ["action", "campaign_name", "adset_name", "spend", "purchases",
                         "revenue", "profit", "roas", "break_even_roas", "reason"]
            styled = board[show_cols].style.apply(
                lambda row: [f"color: {ACTION_COLOR.get(row['action'], '')}; font-weight:600"
                             if c == "action" else "" for c in show_cols],
                axis=1,
            ).format({"spend": "${:.2f}", "revenue": "${:.2f}", "profit": "${:.2f}",
                      "roas": "{:.2f}", "break_even_roas": "{:.2f}"})
            st.dataframe(styled, use_container_width=True, hide_index=True)
        if not show_off and n_off:
            st.caption(f"{n_off} paused ad set(s) hidden. Toggle 'Show paused' to view them.")

    # --- profit by niche (what to double down on) ---
    st.subheader("Profit by niche")
    nf = queries.niche_pnl(since, until)
    if nf.empty:
        st.info("No data in this window yet.")
    else:
        bar = alt.Chart(nf).mark_bar().encode(
            y=alt.Y("theme:N", sort="-x", title=None),
            x=alt.X("profit:Q", title="Net profit ($)"),
            color=alt.condition("datum.profit >= 0", alt.value("#1a7f37"), alt.value("#cf222e")),
            tooltip=[alt.Tooltip("theme:N", title="Niche"),
                     alt.Tooltip("rev:Q", title="Revenue", format="$,.0f"),
                     alt.Tooltip("ad_spend:Q", title="Ad spend", format="$,.0f"),
                     alt.Tooltip("profit:Q", title="Net profit", format="$,.0f")],
        )
        st.altair_chart(bar.properties(height=max(220, 34 * len(nf))), use_container_width=True)
        st.caption("Revenue grouped by shirt niche, ad spend by ad-set niche. Approximate until "
                   "ads carry `utm_campaign=shirt_<id>`; 'other' = uncategorized.")
        disp = nf.rename(columns={"rev": "revenue", "cogs": "printify", "ad_spend": "ad spend",
                                  "profit": "net profit", "margin": "margin %"})
        st.dataframe(
            disp[["theme", "revenue", "printify", "fees", "ad spend", "net profit", "margin %", "units"]]
            .style.format({"revenue": "${:,.0f}", "printify": "${:,.0f}", "fees": "${:,.0f}",
                           "ad spend": "${:,.0f}", "net profit": "${:,.0f}", "units": "{:.0f}"}),
            use_container_width=True, hide_index=True,
        )

    # --- per-shirt profit ---
    st.subheader("Per-shirt profit")
    shirts = queries.shirt_profit()
    if shirts.empty:
        st.info("No shirts yet.")
    else:
        themes = ["(all)"] + sorted(t for t in shirts["theme"].dropna().unique())
        pick = st.selectbox("Filter theme", themes)
        sview = shirts if pick == "(all)" else shirts[shirts["theme"] == pick]
        st.dataframe(sview, use_container_width=True, hide_index=True, height=320)

    # --- AI design ideas ---
    st.subheader("AI design ideas")
    if not cfg.anthropic_api_key:
        st.info("Set `ANTHROPIC_API_KEY` in `.env` to generate new design ideas.")
    else:
        mode_label = st.radio(
            "ideas mode",
            ["Diversify (new niches)", "Extend winners (fresh F1/proven angles)"],
            horizontal=True, label_visibility="collapsed",
        )
        mode = "diversify" if mode_label.startswith("Diversify") else "extend"
        st.caption("Claude proposes original concepts (never duplicates your catalog), using your "
                   "per-niche demand signal. Diversify = grow beyond F1; Extend = fresh proven angles.")
        if st.button("✨ Generate ideas"):
            with st.spinner("Thinking up concepts..."):
                from tshirt.insights import generate_design_ideas
                try:
                    with connect(cfg) as conn:
                        ideas = generate_design_ideas(conn, cfg, mode=mode)
                    st.session_state["ideas"] = [i.model_dump() for i in ideas]
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not generate ideas: {exc}")
        for idea in st.session_state.get("ideas", []):
            risk_color = {"low": "#1a7f37", "medium": "#9a6700", "high": "#cf222e"}[idea["ip_risk"]]
            with st.container(border=True):
                st.markdown(
                    f"**{idea['slogan']}**  ·  _{idea['theme']}_  "
                    f"<span style='color:{risk_color}'>● IP risk: {idea['ip_risk']}</span>",
                    unsafe_allow_html=True,
                )
                st.write(idea["concept"])
                st.caption(idea["rationale"])
        if st.session_state.get("ideas"):
            md = "\n\n".join(
                f"## {i['slogan']} ({i['theme']}, IP risk {i['ip_risk']})\n"
                f"{i['concept']}\n\n_{i['rationale']}_"
                for i in st.session_state["ideas"]
            )
            st.download_button("Download ideas (markdown)", md, "design-ideas.md")

    # --- footer ---
    sync = queries.last_sync()
    if sync:
        when = sync["finished_at"] or sync["started_at"]
        st.caption(f"Last data sync: {when} ({sync['status']})")
    else:
        st.caption("No sync has run yet. Run `tshirt-nightly`.")


main()
