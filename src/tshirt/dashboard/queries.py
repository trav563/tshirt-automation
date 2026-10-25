"""Cached DataFrame read helpers for the dashboard.

All queries are read-only and cached for 5 minutes; the sidebar "Refresh data"
button clears the cache. The nightly job is what actually refreshes the data.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from ..config import Config
from ..db.connection import connect


def _df(sql: str, params: tuple = ()) -> pd.DataFrame:
    with connect() as conn:
        return pd.read_sql_query(sql, conn, params=params)


@st.cache_data(ttl=300)
def kpis(since: str, until: str) -> dict:
    spend_rev = _df(
        """
        select coalesce(sum(spend),0) ad_spend,
               coalesce(sum(purchase_value),0) meta_rev
        from ad_insights where date between %s and %s
        """,
        (since, until),
    ).iloc[0]
    shop = _df(
        "select coalesce(sum(net_revenue),0) rev, count(*) orders "
        "from orders where created_at >= %s and created_at::date <= %s",
        (since, until),
    ).iloc[0]
    profit = _df(
        "select coalesce(sum(net_profit),0) net, count(*) shirts from shirt_profit"
    ).iloc[0]
    spend = float(spend_rev["ad_spend"])
    return {
        "ad_spend": spend,
        "meta_revenue": float(spend_rev["meta_rev"]),
        "shopify_revenue": float(shop["rev"]),
        "orders": int(shop["orders"]),
        "net_profit": float(profit["net"]),
        "shirts": int(profit["shirts"]),
        "mer": round(float(shop["rev"]) / spend, 2) if spend > 0 else None,
        "meta_roas": round(float(spend_rev["meta_rev"]) / spend, 2) if spend > 0 else None,
    }


@st.cache_data(ttl=300)
def operating_pnl(since: str, until: str) -> dict:
    """True account-level operating profit for the window — every cost from the
    platform data:
        net revenue (after discounts + ALL refunds, incl shipping, excl sales tax)
        − Printify cost (production + shipping + tax)
        − real Shopify Payments fees
        − all Meta ad spend
        − fixed overhead (Shopify plan + Printify sub + apps), prorated
    """
    from datetime import date

    p = _df(
        """
        select coalesce(sum(li.revenue),0)          as net_revenue,
               coalesce(sum(li.shipping_revenue),0) as shipping_rev,
               coalesce(sum(li.printify_cost),0)    as cogs,
               coalesce(sum(li.fee),0)              as fees
        from order_line_items li
        join orders o on o.shopify_order_id = li.shopify_order_id
        where o.created_at >= %s and o.created_at::date <= %s
        """,
        (since, until),
    ).iloc[0]
    refunds = float(
        _df("select coalesce(sum(refunds),0) r from orders "
            "where created_at >= %s and created_at::date <= %s", (since, until)).iloc[0]["r"]
    )
    ad = float(
        _df("select coalesce(sum(spend),0) s from ad_insights where date between %s and %s",
            (since, until)).iloc[0]["s"]
    )
    cfg = Config.load()
    window_days = max((date.fromisoformat(until) - date.fromisoformat(since)).days, 1)
    fixed = round(cfg.monthly_fixed_costs / 30.0 * window_days, 2)

    revenue = float(p["net_revenue"])
    cogs, fees = float(p["cogs"]), float(p["fees"])
    profit = round(revenue - cogs - fees - ad - fixed, 2)
    return {
        "revenue": revenue,
        "shipping_rev": float(p["shipping_rev"]),
        "refunds": refunds,
        "cogs": cogs,
        "fees": fees,
        "ad_spend": ad,
        "fixed": fixed,
        "profit": profit,
        "margin_pct": round(100 * profit / revenue, 1) if revenue else None,
    }


@st.cache_data(ttl=300)
def daily_trend(since: str) -> pd.DataFrame:
    spend = _df(
        "select date, sum(spend) ad_spend from ad_insights where date >= %s group by date",
        (since,),
    )
    rev = _df(
        "select created_at::date date, sum(total_price) revenue "
        "from orders where created_at >= %s group by 1",
        (since,),
    )
    if spend.empty and rev.empty:
        return pd.DataFrame(columns=["date", "ad_spend", "revenue"])
    out = pd.merge(spend, rev, on="date", how="outer").fillna(0).sort_values("date")
    return out


@st.cache_data(ttl=300)
def daily_pnl(since: str, until: str) -> pd.DataFrame:
    """Daily net profit time series: net revenue − Printify − fees − ad spend −
    prorated fixed overhead, per day. Orders bucketed by order date, ad spend by
    insight date."""
    o = _df(
        """
        select o.created_at::date           as d,
               sum(li.revenue)               as rev,
               sum(coalesce(li.printify_cost,0)) as cogs,
               sum(coalesce(li.fee,0))           as fees
        from order_line_items li
        join orders o on o.shopify_order_id = li.shopify_order_id
        where o.created_at >= %s and o.created_at::date <= %s
        group by 1
        """,
        (since, until),
    )
    a = _df("select date as d, sum(spend) as ad from ad_insights "
            "where date between %s and %s group by 1", (since, until))
    if o.empty and a.empty:
        return pd.DataFrame(columns=["date", "profit"])
    df = pd.merge(o, a, on="d", how="outer").fillna(0).sort_values("d").rename(columns={"d": "date"})
    df["date"] = pd.to_datetime(df["date"])
    daily_fixed = Config.load().monthly_fixed_costs / 30.0
    df["profit"] = (df["rev"] - df["cogs"] - df["fees"] - df["ad"] - daily_fixed).round(2)
    return df


@st.cache_data(ttl=300)
def shirt_profit() -> pd.DataFrame:
    return _df(
        """
        select shirt_id, title, theme, funnel_status, revenue, printify_cost,
               fees, ad_spend, net_profit, roas, units
        from shirt_profit
        order by net_profit desc nulls last
        """
    )


@st.cache_data(ttl=300)
def theme_profit() -> pd.DataFrame:
    return _df(
        """
        select theme, shirts, revenue, printify_cost, fees, ad_spend, net_profit, roas
        from theme_profit
        """
    )


@st.cache_data(ttl=300)
def niche_pnl(since: str, until: str) -> pd.DataFrame:
    """Per-niche P&L for the window. Revenue/cost grouped by each shirt's niche;
    ad spend grouped by each ad set's niche (classified from campaign/ad-set name).
    Both use the same niche vocabulary; everything uncategorized rolls to 'other'.
    Approximate attribution until ads carry utm_campaign=shirt_<id>."""
    from ..themes import NICHE_KEYS, classify_theme

    niche_set = set(NICHE_KEYS)
    canon = lambda t: t if t in niche_set else "other"  # noqa: E731

    prod = _df(
        """
        select coalesce(s.theme,'other')          as theme,
               sum(li.revenue)                     as rev,
               sum(coalesce(li.printify_cost,0))   as cogs,
               sum(coalesce(li.fee,0))             as fees,
               sum(li.quantity)                    as units
        from order_line_items li
        join orders o on o.shopify_order_id = li.shopify_order_id
        join shirts s on s.shirt_id = coalesce(li.shirt_id, li.shopify_product_id)
        where o.created_at >= %s and o.created_at::date <= %s
        group by 1
        """,
        (since, until),
    )
    for c in ("rev", "cogs", "fees", "units"):
        prod[c] = prod[c].astype(float)
    prod["theme"] = prod["theme"].map(canon)
    prod = prod.groupby("theme", as_index=False).sum()

    ads = _df(
        "select coalesce(campaign_name,'') || ' ' || coalesce(adset_name,'') as name, "
        "sum(spend) as ad_spend from ad_insights where date between %s and %s group by 1",
        (since, until),
    )
    if not ads.empty:
        ads["ad_spend"] = ads["ad_spend"].astype(float)
        ads["theme"] = ads["name"].map(lambda n: canon(classify_theme(None, None, n)))
        ads = ads.groupby("theme", as_index=False)["ad_spend"].sum()
    else:
        ads = pd.DataFrame({"theme": [], "ad_spend": []})

    df = prod.merge(ads, on="theme", how="outer")
    for c in ("rev", "cogs", "fees", "units", "ad_spend"):
        if c not in df.columns:
            df[c] = 0.0
    df = df.fillna(0.0)
    df["profit"] = (df["rev"] - df["cogs"] - df["fees"] - df["ad_spend"]).round(2)
    df["margin"] = df.apply(
        lambda r: round(100 * r["profit"] / r["rev"], 1) if r["rev"] else None, axis=1)
    return df.sort_values("profit", ascending=False).reset_index(drop=True)


@st.cache_data(ttl=300)
def last_sync() -> dict | None:
    return _latest_sync()


def live_sync() -> dict | None:
    """Uncached twin of last_sync(), for polling a run that is in progress.

    last_sync() is cached for 5 minutes, which would freeze a progress banner.
    """
    return _latest_sync()


def _latest_sync() -> dict | None:
    df = _df(
        "select started_at, finished_at, status, detail "
        "from sync_runs order by id desc limit 1"
    )
    if df.empty:
        return None
    r = df.iloc[0]
    return {
        "started_at": r["started_at"],
        "finished_at": r["finished_at"],
        "status": r["status"],
        "detail": r["detail"] or {},
    }


@st.cache_data(ttl=300)
def adset_launches(since: str, until: str, min_spend: float = 50.0) -> pd.DataFrame:
    """Ad sets whose first-ever recorded spend falls inside the window (their
    'launch'), for marking on the timeline. Limited to meaningful spenders."""
    df = _df(
        """
        select coalesce(adset_name, meta_ad_set_id) as name,
               min(date) as launch,
               sum(spend) as spend
        from ad_insights
        group by 1
        having min(date) between %s and %s and sum(spend) > %s
        order by min(date)
        """,
        (since, until, min_spend),
    )
    return df.head(12)


@st.cache_data(ttl=300)
@st.cache_data(ttl=300)
def uncosted_revenue(since: str, until: str) -> dict:
    """Revenue in the window from order lines with no Printify cost attached.

    Printify's API returns no orders from before late 2024, so the older part of
    the Shopify history carries revenue with no COGS against it and any profit
    figure spanning it is overstated. The boundary is read from the data rather
    than hardcoded, so it stays right as the history changes.
    """
    row = _df(
        """
        select coalesce(sum(li.revenue), 0)          as revenue,
               count(distinct o.shopify_order_id)    as orders
        from order_line_items li
        join orders o on o.shopify_order_id = li.shopify_order_id
        where o.created_at >= %s and o.created_at::date <= %s
          and coalesce(li.printify_cost, 0) = 0
        """,
        (since, until),
    ).iloc[0]
    starts = _df(
        """
        select min(o.created_at)::date d
        from order_line_items li
        join orders o on o.shopify_order_id = li.shopify_order_id
        where coalesce(li.printify_cost, 0) > 0
        """
    ).iloc[0]["d"]
    return {
        "revenue": float(row["revenue"]),
        "orders": int(row["orders"]),
        "cost_data_starts": str(starts) if starts is not None else None,
    }


def earliest_order_date() -> str:
    df = _df("select min(created_at)::date d from orders")
    val = df.iloc[0]["d"] if not df.empty else None
    return str(val) if val is not None else (date.today() - timedelta(days=60)).isoformat()


def default_window_days(cfg: Config) -> int:
    return cfg.sync_lookback_days


def since_for(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()
