"""Analysis + recommendation engine.

Reads the ingested Meta + Shopify + Printify data from Postgres and emits a
scale / hold / kill / investigate / gather recommendation per ad set, plus an
account-level marketing-efficiency (MER) check from Shopify ground truth.

Run after `tshirt-nightly`:
    tshirt-recommend

Output: a ranked terminal table and a dated data/recommendations-YYYY-MM-DD.md.

Profitability uses REAL realized PER-ORDER economics from actual orders, because
Meta counts one "purchase" per order, not one per shirt: order cost = Printify
production + shipping + tax per order, order value = product + customer-paid
shipping per order. An order holds more than one shirt on average, so per-unit
figures would undercharge COGS on every order. When an ad set is mapped to a shirt
(via utm_campaign=shirt_<id>) it uses that shirt's numbers; otherwise it uses the
account-blended real numbers. DEFAULT_UNIT_COGS / DEFAULT_PRICE are used only as a
last resort when there is no order history at all, and assume one shirt per order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import psycopg
from tabulate import tabulate

from ..config import PROJECT_ROOT, Config
from ..db.connection import connect

# Payment/Shopify fee, mirrors jobs/nightly.py. Last-resort fallback only: the
# real per-order fee comes from orders.fee (Shopify Payments balance transactions).
FEE_RATE = 0.029
FEE_FIXED = 0.30

# Decision thresholds (per ad set, over the lookback window).
MIN_SPEND_FOR_DECISION = 20.0   # below this we don't have enough signal to kill
SCALE_ROAS_MULTIPLE = 1.30      # scale when ROAS >= break-even * this
INVESTIGATE_CLICKS = 40         # this many clicks with no sale => landing/offer problem

ACTIONS = ("SCALE", "HOLD", "INVESTIGATE", "KILL", "GATHER", "OFF")


@dataclass
class AdSetReco:
    campaign_name: str
    adset_name: str
    adset_id: str
    shirt_id: str | None
    title: str | None
    theme: str | None
    spend: float
    clicks: int
    purchases: int
    revenue: float          # real Shopify revenue when attributed, else Meta's pixel value
    order_cogs: float       # realized cost of an average order, not of one shirt
    cogs: float
    fees: float
    profit: float
    roas: float | None
    break_even_roas: float
    action: str
    reason: str
    effective_status: str | None = None
    attribution: str = "meta"   # "shopify" = this ad set's own tagged orders


def _break_even_roas(order_cogs: float, order_value: float, order_fee: float) -> float:
    """ROAS at which an order's contribution exactly covers ad spend."""
    contribution = order_value - order_cogs - order_fee
    if contribution <= 0:
        return float("inf")
    return round(order_value / contribution, 2)


def _classify(spend: float, clicks: int, purchases: int, profit: float,
              roas: float | None, break_even: float) -> tuple[str, str]:
    if spend < MIN_SPEND_FOR_DECISION:
        return "GATHER", f"only ${spend:.0f} spent; need >= ${MIN_SPEND_FOR_DECISION:.0f} to judge"
    if purchases == 0:
        if clicks >= INVESTIGATE_CLICKS:
            return "INVESTIGATE", f"{clicks} clicks, 0 sales — landing page / offer / price issue"
        return "KILL", f"${spend:.0f} spent, 0 sales"
    if profit <= 0:
        return "KILL", f"unprofitable: ROAS {roas:.2f} below break-even {break_even:.2f}"
    if roas is not None and roas >= break_even * SCALE_ROAS_MULTIPLE:
        return "SCALE", f"profitable: ROAS {roas:.2f} >= scale bar {break_even * SCALE_ROAS_MULTIPLE:.2f} (+budget)"
    return "HOLD", f"profitable but thin: ROAS {roas:.2f} vs break-even {break_even:.2f}"


def _adset_rows(conn: psycopg.Connection, since: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select
                coalesce(ai.campaign_name, '(no campaign)') as campaign_name,
                coalesce(ai.adset_name, ai.meta_ad_set_id)  as adset_name,
                ai.meta_ad_set_id,
                ai.shirt_id,
                s.title, s.theme, s.printify_base_cost, s.price,
                ms.effective_status,
                sum(ai.spend)          as spend,
                sum(ai.clicks)         as clicks,
                sum(ai.purchases)      as purchases,
                sum(ai.purchase_value) as revenue
            from ad_insights ai
            left join shirts s on s.shirt_id = ai.shirt_id
            left join meta_adsets ms on ms.meta_ad_set_id = ai.meta_ad_set_id
            where ai.date >= %s
            group by 1, 2, 3, 4, 5, 6, 7, 8, 9
            having sum(ai.spend) > 0
            order by sum(ai.spend) desc
            """,
            (since,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _real_order_economics(
    conn: psycopg.Connection, since: str
) -> tuple[float | None, float | None, float | None, dict]:
    """Derive true per-order cost, value and fee from actual orders in the window.

    Meta reports one purchase per ORDER, so every figure here is per order:
    order cost = realized Printify cost (production + shipping + tax) per order;
    order value = realized revenue (product + shipping) per order; order fee =
    the real Shopify Payments fee per order. Returns the account-blended values
    plus a per-shirt map, all from real data.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select coalesce(li.shirt_id, li.shopify_product_id) as shirt_id,
                   sum(coalesce(li.printify_cost,0))                     as cost,
                   sum(li.revenue)                                       as rev,
                   sum(coalesce(li.fee,0))                               as fee,
                   count(distinct li.shopify_order_id)                   as orders
            from order_line_items li
            join orders o on o.shopify_order_id = li.shopify_order_id
            where o.created_at >= %s
            group by 1
            """,
            (since,),
        )
        rows = cur.fetchall()

        # The blended figures need DISTINCT orders over the whole window: summing
        # the per-shirt counts above would count an order once per shirt in it.
        cur.execute(
            """
            select sum(coalesce(li.printify_cost,0)),
                   sum(li.revenue),
                   sum(coalesce(li.fee,0)),
                   count(distinct li.shopify_order_id)
            from order_line_items li
            join orders o on o.shopify_order_id = li.shopify_order_id
            where o.created_at >= %s
            """,
            (since,),
        )
        tot_cost, tot_rev, tot_fee, tot_orders = cur.fetchone()

    per_shirt: dict[str, tuple[float, float, float]] = {}
    for shirt_id, cost, rev, fee, orders in rows:
        cost, rev, fee = float(cost or 0), float(rev or 0), float(fee or 0)
        orders = int(orders or 0)
        if orders > 0:
            per_shirt[str(shirt_id)] = (round(cost / orders, 2), round(rev / orders, 2),
                                        round(fee / orders, 2))
    tot_cost, tot_rev, tot_fee = float(tot_cost or 0), float(tot_rev or 0), float(tot_fee or 0)
    tot_orders = int(tot_orders or 0)
    blended_cogs = round(tot_cost / tot_orders, 2) if tot_orders else None
    blended_price = round(tot_rev / tot_orders, 2) if tot_orders else None
    blended_fee = round(tot_fee / tot_orders, 2) if tot_orders else None
    return blended_cogs, blended_price, blended_fee, per_shirt


def _shopify_by_adset(conn: psycopg.Connection, since: str) -> dict[str, dict]:
    """Real orders, revenue, cost and fees per ad set, credited by Meta's utm_term.

    Meta's dynamic URL tags put the ad set id in utm_term, so this is what an ad
    set actually sold rather than what the pixel claims it sold.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select o.utm_term                                   as adset_id,
                   count(distinct o.shopify_order_id)           as orders,
                   sum(li.revenue)                              as revenue,
                   sum(coalesce(li.printify_cost,0))            as cogs,
                   sum(coalesce(li.fee,0))                      as fees
            from orders o
            join order_line_items li on li.shopify_order_id = o.shopify_order_id
            where o.created_at >= %s and o.utm_term is not null
            group by 1
            """,
            (since,),
        )
        return {
            str(adset_id): {"orders": int(orders), "revenue": float(rev or 0),
                            "cogs": float(cogs or 0), "fees": float(fees or 0)}
            for adset_id, orders, rev, cogs, fees in cur.fetchall()
        }


def build_recommendations(conn: psycopg.Connection, cfg: Config, since: str) -> list[AdSetReco]:
    # Real per-order economics from actual orders; cfg defaults are last resort only.
    blended_cogs, blended_price, blended_fee, per_shirt = _real_order_economics(conn, since)
    blended_cogs = blended_cogs if blended_cogs is not None else cfg.default_unit_cogs
    blended_price = blended_price if blended_price is not None else cfg.default_price
    if blended_fee is None:
        blended_fee = round(blended_price * FEE_RATE + FEE_FIXED, 2)
    attributed = _shopify_by_adset(conn, since)

    out: list[AdSetReco] = []
    for r in _adset_rows(conn, since):
        spend = float(r["spend"] or 0)
        clicks = int(r["clicks"] or 0)
        real = attributed.get(str(r["meta_ad_set_id"]))
        if real:
            # Ground truth: the orders this ad set's own utm_term actually brought in.
            attribution = "shopify"
            purchases = real["orders"]
            revenue = round(real["revenue"], 2)
            cogs = round(real["cogs"], 2)
            fees = round(real["fees"], 2)
            order_cogs = round(cogs / purchases, 2)
            order_value = round(revenue / purchases, 2)
            order_fee = round(fees / purchases, 2)
        else:
            # Nothing tagged to this ad set: fall back to Meta's pixel count priced
            # at blended economics. purchases counts ORDERS, so the rates are per order.
            attribution = "meta"
            purchases = int(r["purchases"] or 0)
            revenue = float(r["revenue"] or 0)
            sid = str(r["shirt_id"]) if r["shirt_id"] else None
            if sid and sid in per_shirt:
                order_cogs, order_value, order_fee = per_shirt[sid]
            else:
                order_cogs, order_value, order_fee = blended_cogs, blended_price, blended_fee
            cogs = round(order_cogs * purchases, 2)
            fees = round(order_fee * purchases, 2)
        profit = round(revenue - cogs - fees - spend, 2)
        roas = round(revenue / spend, 2) if spend > 0 else None
        break_even = _break_even_roas(order_cogs, order_value, order_fee)
        action, reason = _classify(spend, clicks, purchases, profit, roas, break_even)
        if attribution == "meta" and purchases:
            reason += " (meta-estimated: no tagged orders)"

        # An ad set that isn't live needs no scale/hold/kill action — it's already off.
        eff = r.get("effective_status")
        if eff and eff != "ACTIVE":
            action = "OFF"
            reason = f"not running ({eff.lower().replace('_', ' ')}) — already off"

        out.append(
            AdSetReco(
                campaign_name=r["campaign_name"], adset_name=r["adset_name"],
                adset_id=r["meta_ad_set_id"], shirt_id=r["shirt_id"],
                title=r["title"], theme=r["theme"], spend=round(spend, 2),
                clicks=clicks, purchases=purchases, revenue=round(revenue, 2),
                order_cogs=round(order_cogs, 2), cogs=cogs, fees=fees, profit=profit,
                roas=roas, break_even_roas=break_even, action=action, reason=reason,
                effective_status=eff, attribution=attribution,
            )
        )
    # Sort by action priority, then by money at stake.
    order = {a: i for i, a in enumerate(ACTIONS)}
    out.sort(key=lambda x: (order.get(x.action, 99), -x.spend))
    return out


def account_summary(conn: psycopg.Connection, since: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("select coalesce(sum(spend),0), coalesce(sum(purchase_value),0) "
                    "from ad_insights where date >= %s", (since,))
        spend, meta_rev = cur.fetchone()
        cur.execute("select coalesce(sum(net_revenue),0) from orders where created_at >= %s", (since,))
        shop_rev = cur.fetchone()[0]
    spend = float(spend)
    return {
        "spend": round(spend, 2),
        "meta_revenue": round(float(meta_rev), 2),
        "shopify_revenue": round(float(shop_rev), 2),
        "mer": round(float(shop_rev) / spend, 2) if spend > 0 else None,
        "meta_roas": round(float(meta_rev) / spend, 2) if spend > 0 else None,
    }


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def _table(recos: list[AdSetReco]) -> str:
    rows = [
        [r.action, r.campaign_name[:20], r.adset_name[:24], r.spend, r.purchases,
         r.revenue, r.profit, (f"{r.roas:.2f}" if r.roas is not None else "-"),
         f"{r.break_even_roas:.2f}"]
        for r in recos
    ]
    return tabulate(
        rows,
        headers=["action", "campaign", "ad set", "spend", "sales", "rev",
                 "profit", "roas", "b/e"],
        floatfmt=".2f",
    )


def _markdown(recos: list[AdSetReco], summary: dict, since: str, until: str) -> str:
    lines = [
        f"# Ad recommendations — {until}",
        f"\nWindow: {since} to {until}\n",
        "## Account summary (Shopify ground truth)",
        f"- Ad spend: **${summary['spend']:.2f}**",
        f"- Shopify revenue (all sources): **${summary['shopify_revenue']:.2f}**",
        f"- MER (Shopify rev / ad spend): **{summary['mer'] if summary['mer'] is not None else 'n/a'}**",
        f"- Meta-reported revenue: ${summary['meta_revenue']:.2f} "
        f"(pixel ROAS {summary['meta_roas'] if summary['meta_roas'] is not None else 'n/a'})",
        "",
        "> MER is blended (all revenue vs all ad spend). Per-ad-set numbers below come "
        "from the orders each ad set's `utm_term` actually brought in; rows marked "
        "meta-estimated had no tagged orders and fall back to pixel attribution.",
        "",
    ]
    for action in ACTIONS:
        group = [r for r in recos if r.action == action]
        if not group:
            continue
        lines.append(f"## {action} ({len(group)})")
        for r in group:
            who = r.title or r.adset_name
            lines.append(
                f"- **{who}** — spend ${r.spend:.2f}, {r.purchases} sales, "
                f"profit ${r.profit:.2f}"
                + (f", ROAS {r.roas:.2f}" if r.roas is not None else "")
                + f". {r.reason}"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    cfg = Config.load()
    missing = cfg.missing_required()
    if missing:
        print("Cannot run — missing required .env keys: " + ", ".join(missing))
        return 1

    since = (date.today() - timedelta(days=cfg.sync_lookback_days)).isoformat()
    until = date.today().isoformat()

    with connect(cfg) as conn:
        summary = account_summary(conn, since)
        recos = build_recommendations(conn, cfg, since)

    print(f"\nAd recommendations — window {since} to {until}")
    print(
        f"  spend ${summary['spend']:.2f} | Shopify rev ${summary['shopify_revenue']:.2f} "
        f"| MER {summary['mer']} | Meta pixel ROAS {summary['meta_roas']}\n"
    )
    print(_table(recos) if recos else "  (no ad-set data yet — run tshirt-nightly first)")

    out_dir = PROJECT_ROOT / "data"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"recommendations-{until}.md"
    out_path.write_text(_markdown(recos, summary, since, until))
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
