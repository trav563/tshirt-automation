"""Nightly measurement job.

Pulls Shopify (revenue), Printify (fulfillment cost), and Meta (ad spend) into
Supabase Postgres, joins everything on the shirt_id / shopify_order_id
conventions, then prints the ranked per-shirt and per-theme profit + ROAS table.

Run:  tshirt-migrate   (once, to create tables)
      tshirt-nightly   (every night)

Designed to run as soon as the .env credentials are filled in.
"""

from __future__ import annotations

from datetime import date, timedelta

import psycopg
from psycopg.types.json import Jsonb
from tabulate import tabulate

from ..clients.meta import MetaClient, parse_purchase_value, parse_purchases
from ..clients.printify import PrintifyClient, order_costs, product_base_cost
from ..clients.shopify import ShopifyClient, ShopifyError, gid_numeric
from ..config import Config
from ..db.connection import connect
from ..themes import classify_theme

# Shopify Payments / standard online card rate. Used to estimate fees.
FEE_RATE = 0.029
FEE_FIXED = 0.30

# Two syncs at once (dashboard button vs launchd vs Refresh Now.command) contend
# on the same upserts, and Supabase's 2min statement_timeout then kills the loser
# mid-run. This lock is session-scoped: it releases on close AND on crash/kill,
# so a dead run can never wedge the next one out.
NIGHTLY_LOCK_KEY = 0x74736869  # 'tshi'

# Stage labels, in the order main() runs them, for the dashboard's live banner.
_STAGES = (
    "shopify products",
    "payment fees",
    "shopify orders",
    "printify products",
    "printify orders",
    "allocating costs",
    "meta ad-sets",
    "meta ad-set days",
)

_SHOPIFY_STATUS_TO_FUNNEL = {
    "ACTIVE": "winner",
    "DRAFT": "staged",
    "ARCHIVED": "killed",
}


# ---------------------------------------------------------------------------
# small parse helpers
# ---------------------------------------------------------------------------
def _money(node: dict | None) -> float:
    """Pull a float out of a Shopify MoneySet: {shopMoney: {amount: '12.34'}}."""
    if not node:
        return 0.0
    try:
        return float(node["shopMoney"]["amount"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def _progress(conn: psycopg.Connection, run_id: int | None, stage: int) -> None:
    """Record the stage about to run, so the dashboard can show live progress."""
    if run_id is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            "update sync_runs set detail = coalesce(detail, '{}'::jsonb) || %s where id = %s",
            (
                Jsonb({"stage": stage, "stage_name": _STAGES[stage - 1],
                       "stages_total": len(_STAGES)}),
                run_id,
            ),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Shopify
# ---------------------------------------------------------------------------
def sync_shopify_products(conn: psycopg.Connection, client: ShopifyClient) -> int:
    rows = 0
    with conn.cursor() as cur:
        for p in client.iter_products():
            shirt_id = gid_numeric(p["id"])
            variants = (p.get("variants") or {}).get("nodes") or []
            price = float(variants[0]["price"]) if variants and variants[0].get("price") else None
            theme = classify_theme(p.get("tags"), p.get("productType"), p.get("title"))
            funnel = _SHOPIFY_STATUS_TO_FUNNEL.get(p.get("status"), "generated")
            cur.execute(
                """
                insert into shirts (shirt_id, title, theme, shopify_product_id,
                    shopify_status, funnel_status, price, created_at, updated_at, raw)
                values (%s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
                on conflict (shirt_id) do update set
                    title = excluded.title,
                    theme = excluded.theme,
                    shopify_status = excluded.shopify_status,
                    funnel_status = excluded.funnel_status,
                    price = excluded.price,
                    updated_at = now(),
                    raw = excluded.raw
                """,
                (
                    shirt_id, p.get("title"), theme, shirt_id,
                    p.get("status"), funnel, price, p.get("createdAt"), Jsonb(p),
                ),
            )
            rows += 1
    conn.commit()
    return rows


def fetch_real_fees(client: ShopifyClient, since: str) -> dict | None:
    """Map shopify_order_id -> real Shopify Payments processing fee for the window.

    Returns None if the payouts scope is missing (caller falls back to the
    estimate); an empty dict if the store isn't on Shopify Payments.
    """
    fees: dict[str, float] = {}
    try:
        for tx in client.iter_balance_transactions(since):
            order_id = gid_numeric((tx.get("associatedOrder") or {}).get("id"))
            if not order_id:
                continue
            fee_amt = float((tx.get("fee") or {}).get("amount") or 0)
            fees[order_id] = round(fees.get(order_id, 0.0) + fee_amt, 2)
    except ShopifyError as exc:
        if "ACCESS_DENIED" in str(exc).upper():
            return None
        raise
    return fees


def sync_shopify_orders(
    conn: psycopg.Connection, client: ShopifyClient, since: str,
    real_fees: dict | None = None,
) -> int:
    real_fees = real_fees or {}
    rows = 0
    with conn.cursor() as cur:
        for o in client.iter_orders(created_at_min=since):
            order_id = gid_numeric(o["id"])
            total = _money(o.get("totalPriceSet"))
            shipping_rev = _money(o.get("totalShippingPriceSet"))  # customer-paid shipping (gross)
            refunds = _money(o.get("totalRefundedSet"))
            tax = _money(o.get("currentTotalTaxSet"))  # sales tax collected (pass-through)
            # NET revenue actually kept = amount received minus refunds, minus pass-through
            # sales tax. netPaymentSet already nets ALL refunds (full, partial, shipping).
            net_revenue = round(_money(o.get("netPaymentSet")) - tax, 2)
            # Prefer the real Shopify Payments fee; fall back to the standard estimate.
            fee = real_fees.get(order_id)
            if fee is None:
                fee = round(total * FEE_RATE + FEE_FIXED, 2) if total else 0.0
            utm = ((o.get("customerJourneySummary") or {}).get("lastVisit") or {}).get(
                "utmParameters"
            ) or {}
            cur.execute(
                """
                insert into orders (shopify_order_id, name, created_at, currency,
                    subtotal, total_discounts, total_price, shipping_revenue,
                    net_revenue, refunds, total_tax, fee,
                    utm_campaign, utm_source, utm_medium, raw)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                on conflict (shopify_order_id) do update set
                    total_price = excluded.total_price,
                    subtotal = excluded.subtotal,
                    total_discounts = excluded.total_discounts,
                    shipping_revenue = excluded.shipping_revenue,
                    net_revenue = excluded.net_revenue,
                    refunds = excluded.refunds,
                    total_tax = excluded.total_tax,
                    fee = excluded.fee,
                    utm_campaign = excluded.utm_campaign,
                    utm_source = excluded.utm_source,
                    utm_medium = excluded.utm_medium,
                    raw = excluded.raw
                """,
                (
                    order_id, o.get("name"), o.get("createdAt"), o.get("currencyCode"),
                    _money(o.get("currentSubtotalPriceSet")),
                    _money(o.get("totalDiscountsSet")), total, shipping_rev,
                    net_revenue, refunds, tax, fee,
                    utm.get("campaign"), utm.get("source"), utm.get("medium"), Jsonb(o),
                ),
            )

            line_nodes = (o.get("lineItems") or {}).get("nodes") or []
            # Allocate the order's NET revenue (after discounts + refunds, excl tax)
            # across lines by their gross product share, so per-shirt revenue reflects
            # everything the customer actually kept and paid.
            gross = sum(_money(li.get("discountedTotalSet")) for li in line_nodes)
            for li in line_nodes:
                line_id = gid_numeric(li["id"])
                product_id = gid_numeric((li.get("product") or {}).get("id"))
                share = (_money(li.get("discountedTotalSet")) / gross) if gross else 0.0
                line_rev = round(net_revenue * share, 2)  # net of discounts + refunds, excl tax
                # Allocate the order fee and shipping income across lines by the same share.
                line_fee = round(fee * share, 2)
                line_ship = round(shipping_rev * share, 2)
                cur.execute(
                    """
                    insert into order_line_items (line_item_id, shopify_order_id,
                        shirt_id, shopify_product_id, variant_id, quantity, revenue,
                        shipping_revenue, fee)
                    values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    on conflict (line_item_id) do update set
                        shirt_id = excluded.shirt_id,
                        shopify_product_id = excluded.shopify_product_id,
                        variant_id = excluded.variant_id,
                        quantity = excluded.quantity,
                        revenue = excluded.revenue,
                        shipping_revenue = excluded.shipping_revenue,
                        fee = excluded.fee
                    """,
                    (
                        line_id, order_id, product_id, product_id,
                        gid_numeric((li.get("variant") or {}).get("id")),
                        li.get("quantity"), line_rev, line_ship, line_fee,
                    ),
                )
            rows += 1
    conn.commit()
    return rows


# ---------------------------------------------------------------------------
# Printify
# ---------------------------------------------------------------------------
def sync_printify_products(conn: psycopg.Connection, client: PrintifyClient) -> int:
    """Attach Printify product id + base cost onto the matching shirt."""
    rows = 0
    with conn.cursor() as cur:
        for p in client.iter_products():
            shopify_product_id = str((p.get("external") or {}).get("id") or "").strip()
            if not shopify_product_id:
                continue
            cur.execute(
                """
                update shirts
                   set printify_product_id = %s,
                       printify_base_cost = %s,
                       updated_at = now()
                 where shirt_id = %s
                """,
                (str(p.get("id")), product_base_cost(p), shopify_product_id),
            )
            rows += cur.rowcount
    conn.commit()
    return rows


def sync_printify_orders(conn: psycopg.Connection, client: PrintifyClient) -> int:
    rows = 0
    with conn.cursor() as cur:
        for o in client.iter_orders():
            line_cost, shipping = order_costs(o)
            # Printify stores the Shopify order id in metadata.shop_order_id (not external_id).
            shop_order_id = str((o.get("metadata") or {}).get("shop_order_id") or "") or None
            cur.execute(
                """
                insert into printify_orders (printify_order_id, shopify_order_id,
                    line_items_cost, shipping_cost, status, raw)
                values (%s,%s,%s,%s,%s,%s)
                on conflict (printify_order_id) do update set
                    shopify_order_id = excluded.shopify_order_id,
                    line_items_cost = excluded.line_items_cost,
                    shipping_cost = excluded.shipping_cost,
                    status = excluded.status,
                    raw = excluded.raw
                """,
                (
                    str(o.get("id")), shop_order_id,
                    line_cost, shipping, o.get("status"), Jsonb(o),
                ),
            )
            rows += 1
    conn.commit()
    return rows


def allocate_printify_cost(conn: psycopg.Connection) -> None:
    """Spread each Printify order's full bill (line items + shipping + sales tax)
    across the matching Shopify order's line items, proportional to line revenue.

    Printify bills the merchant for production + shipping + sales tax. total_tax
    lives in the raw payload (it can be eliminated by filing a resale certificate
    with Printify, which is a business action, not a code change)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            with order_rev as (
                select shopify_order_id, sum(revenue) as rev
                from order_line_items group by 1
            ),
            po as (
                select shopify_order_id,
                       sum(coalesce(line_items_cost,0) + coalesce(shipping_cost,0)
                           + coalesce((raw->>'total_tax')::numeric,0) / 100.0) as cost
                from printify_orders
                where shopify_order_id is not null
                group by 1
            )
            update order_line_items li
               set printify_cost = round(po.cost * (li.revenue / nullif(orv.rev,0)), 2)
              from po
              join order_rev orv on orv.shopify_order_id = po.shopify_order_id
             where po.shopify_order_id = li.shopify_order_id
            """
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------
def sync_meta_adsets(conn: psycopg.Connection, client: MetaClient) -> int:
    """Upsert each ad set's current live status."""
    rows = 0
    with conn.cursor() as cur:
        for a in client.iter_adsets():
            cur.execute(
                """
                insert into meta_adsets (meta_ad_set_id, name, status, effective_status, updated_at)
                values (%s,%s,%s,%s, now())
                on conflict (meta_ad_set_id) do update set
                    name = excluded.name,
                    status = excluded.status,
                    effective_status = excluded.effective_status,
                    updated_at = now()
                """,
                (str(a.get("id")), a.get("name"), a.get("status"), a.get("effective_status")),
            )
            rows += 1
    conn.commit()
    return rows


def sync_meta(conn: psycopg.Connection, client: MetaClient, since: str, until: str) -> int:
    shirt_map = client.adset_shirt_map()
    rows = 0
    with conn.cursor() as cur:
        for row in client.iter_adset_insights(since, until):
            adset_id = str(row.get("adset_id"))
            shirt_id = shirt_map.get(adset_id)
            cur.execute(
                """
                insert into ad_insights (meta_ad_set_id, adset_name, campaign_id,
                    campaign_name, shirt_id, date, spend,
                    impressions, clicks, purchases, purchase_value, raw)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                on conflict (meta_ad_set_id, date) do update set
                    adset_name = excluded.adset_name,
                    campaign_id = excluded.campaign_id,
                    campaign_name = excluded.campaign_name,
                    shirt_id = excluded.shirt_id,
                    spend = excluded.spend,
                    impressions = excluded.impressions,
                    clicks = excluded.clicks,
                    purchases = excluded.purchases,
                    purchase_value = excluded.purchase_value,
                    raw = excluded.raw
                """,
                (
                    adset_id, row.get("adset_name"), row.get("campaign_id"),
                    row.get("campaign_name"), shirt_id, row.get("date_start"),
                    float(row.get("spend") or 0),
                    int(row.get("impressions") or 0), int(row.get("clicks") or 0),
                    parse_purchases(row.get("actions")),
                    parse_purchase_value(row.get("action_values")),
                    Jsonb(row),
                ),
            )
            rows += 1
    conn.commit()
    return rows


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def print_report(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            select shirt_id, coalesce(title,''), coalesce(theme,''), funnel_status,
                   revenue, printify_cost, fees, ad_spend, net_profit, roas, units
            from shirt_profit
            order by net_profit desc nulls last
            """
        )
        shirt_rows = cur.fetchall()
        cur.execute(
            """
            select theme, shirts, revenue, printify_cost, fees, ad_spend, net_profit, roas
            from theme_profit
            """
        )
        theme_rows = cur.fetchall()

    print("\n=== Per-shirt profit / ROAS (top by net profit) ===")
    print(
        tabulate(
            [r[:1] + (r[1][:28],) + r[2:] for r in shirt_rows[:40]],
            headers=["shirt", "title", "theme", "funnel", "rev", "printify",
                     "fees", "adspend", "net", "roas", "units"],
            floatfmt=".2f",
        )
        if shirt_rows
        else "  (no shirts yet)"
    )

    print("\n=== Per-theme rollup ===")
    print(
        tabulate(
            theme_rows,
            headers=["theme", "shirts", "rev", "printify", "fees", "adspend", "net", "roas"],
            floatfmt=".2f",
        )
        if theme_rows
        else "  (no data yet)"
    )


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------
def main() -> int:
    cfg = Config.load()
    missing = cfg.missing_required()
    if missing:
        print("Cannot run — missing required .env keys: " + ", ".join(missing))
        print("Run `tshirt-check` for the full status.")
        return 1

    since = (date.today() - timedelta(days=cfg.sync_lookback_days)).isoformat()
    until = date.today().isoformat()
    print(f"Nightly sync — lookback {cfg.sync_lookback_days}d (since {since})")

    conn = connect(cfg)
    run_id = None
    try:
        # Claim the sync slot before recording a run, so a rejected attempt
        # leaves no trace in sync_runs.
        with conn.cursor() as cur:
            cur.execute("select pg_try_advisory_lock(%s)", (NIGHTLY_LOCK_KEY,))
            if not cur.fetchone()[0]:
                print("Another sync is already running — exiting without changes.")
                return 2
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "insert into sync_runs (status, detail) values ('running', %s) returning id",
                (Jsonb({"since": since, "until": until}),),
            )
            run_id = cur.fetchone()[0]
        conn.commit()

        shopify = ShopifyClient(cfg)
        printify = PrintifyClient(cfg)
        meta = MetaClient(cfg)

        _progress(conn, run_id, 1)
        n_products = sync_shopify_products(conn, shopify)
        print(f"  shopify products: {n_products}")
        _progress(conn, run_id, 2)
        real_fees = fetch_real_fees(shopify, since)
        if real_fees is None:
            print("  payment fees:     ESTIMATE (add read_shopify_payments_payouts scope for real fees)")
        else:
            print(f"  payment fees:     REAL for {len(real_fees)} orders")
        _progress(conn, run_id, 3)
        n_orders = sync_shopify_orders(conn, shopify, since, real_fees)
        print(f"  shopify orders:   {n_orders}")
        _progress(conn, run_id, 4)
        n_pf_products = sync_printify_products(conn, printify)
        print(f"  printify products matched: {n_pf_products}")
        _progress(conn, run_id, 5)
        n_pf_orders = sync_printify_orders(conn, printify)
        print(f"  printify orders:  {n_pf_orders}")
        _progress(conn, run_id, 6)
        allocate_printify_cost(conn)
        _progress(conn, run_id, 7)
        n_adsets = sync_meta_adsets(conn, meta)
        print(f"  meta ad-sets (status): {n_adsets}")
        _progress(conn, run_id, 8)
        n_ads = sync_meta(conn, meta, since, until)
        print(f"  meta ad-set days: {n_ads}")

        detail = {
            "shopify_products": n_products,
            "shopify_orders": n_orders,
            "printify_products_matched": n_pf_products,
            "printify_orders": n_pf_orders,
            "meta_adset_days": n_ads,
        }
        with conn.cursor() as cur:
            cur.execute(
                "update sync_runs set status='ok', finished_at=now(), detail=%s where id=%s",
                (Jsonb({**detail, "since": since, "until": until}), run_id),
            )
        conn.commit()

        print_report(conn)
        return 0
    except Exception as exc:  # noqa: BLE001 — record then re-raise for visibility
        conn.rollback()
        if run_id is not None:
            with conn.cursor() as cur:
                cur.execute(
                    "update sync_runs set status='error', finished_at=now(), detail=%s where id=%s",
                    (Jsonb({"error": str(exc)}), run_id),
                )
            conn.commit()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
