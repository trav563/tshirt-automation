-- T-Shirt Automation: measurement-layer schema (Postgres / Supabase)
--
-- Attribution convention (HANDOFF.md):
--   one shirt = one Shopify product = one Meta ad set = one UTM.
--   shirt_id is the canonical numeric Shopify product id (as text), and every
--   ad set's destination URL carries utm_campaign=shirt_<shirt_id>.
--
-- Idempotent: safe to run repeatedly (create if not exists / create or replace).

-- ---------------------------------------------------------------------------
-- Dimension: one row per shirt
-- ---------------------------------------------------------------------------
create table if not exists shirts (
    shirt_id            text primary key,        -- = numeric Shopify product id
    title               text,
    theme               text,                    -- niche: f1, baseball, nyc, funny, ...
    shopify_product_id  text,                    -- = shirt_id, kept explicit
    shopify_status      text,                    -- ACTIVE | DRAFT | ARCHIVED
    funnel_status       text,                    -- generated|staged|testing|winner|killed
    printify_product_id text,
    printify_base_cost  numeric(10, 2),          -- cheapest enabled variant cost
    price               numeric(10, 2),          -- Shopify retail price
    created_at          timestamptz,
    updated_at          timestamptz default now(),
    raw                 jsonb
);

-- ---------------------------------------------------------------------------
-- Shopify orders (ground-truth revenue) + per-line allocation
-- ---------------------------------------------------------------------------
create table if not exists orders (
    shopify_order_id    text primary key,
    name                text,
    created_at          timestamptz,
    currency            text,
    subtotal            numeric(10, 2),
    total_discounts     numeric(10, 2),
    total_price         numeric(10, 2),
    fee                 numeric(10, 2),          -- payment/Shopify fee: 2.9% + $0.30
    utm_campaign        text,
    utm_source          text,
    utm_medium          text,
    raw                 jsonb
);

create table if not exists order_line_items (
    line_item_id        text primary key,
    shopify_order_id    text references orders (shopify_order_id) on delete cascade,
    shirt_id            text,                    -- resolved from the line's product id
    shopify_product_id  text,
    variant_id          text,
    quantity            integer,
    revenue             numeric(10, 2),          -- discounted line total
    printify_cost       numeric(10, 2),          -- allocated from the matched Printify order
    fee                 numeric(10, 2)           -- allocated share of the order fee
);

create index if not exists idx_oli_order on order_line_items (shopify_order_id);
create index if not exists idx_oli_shirt on order_line_items (shirt_id);

-- Added via ALTER so existing databases pick them up on a re-run of the migration.
-- shipping_revenue: customer-paid shipping (allocated per line, informational).
-- order_line_items.revenue holds the NET revenue kept (netPayment - tax), allocated.
alter table orders add column if not exists shipping_revenue numeric(10, 2);
alter table orders add column if not exists net_revenue numeric(10, 2);   -- netPayment - tax (after refunds)
alter table orders add column if not exists refunds numeric(10, 2);       -- total refunded to customer
alter table orders add column if not exists total_tax numeric(10, 2);     -- sales tax collected (pass-through)
-- Meta's dynamic URL tags: utm_term is the ad set id and utm_content the ad id,
-- which is what lets an order be credited to the ad set that actually sold it.
alter table orders add column if not exists utm_content text;             -- Meta ad id
alter table orders add column if not exists utm_term text;                -- Meta ad set id
create index if not exists idx_orders_utm_term on orders (utm_term);
alter table order_line_items add column if not exists shipping_revenue numeric(10, 2);

-- ---------------------------------------------------------------------------
-- Printify orders (fulfillment cost), joined to Shopify via external_id
-- ---------------------------------------------------------------------------
create table if not exists printify_orders (
    printify_order_id   text primary key,
    shopify_order_id    text,                    -- Printify order.external_id
    line_items_cost     numeric(10, 2),
    shipping_cost       numeric(10, 2),
    status              text,
    raw                 jsonb
);

create index if not exists idx_po_shopify on printify_orders (shopify_order_id);

-- ---------------------------------------------------------------------------
-- Meta ad insights (daily, per ad set)
-- ---------------------------------------------------------------------------
create table if not exists ad_insights (
    meta_ad_set_id      text,
    adset_name          text,
    campaign_id         text,
    campaign_name       text,
    shirt_id            text,
    date                date,
    spend               numeric(10, 2),
    impressions         bigint,
    clicks              bigint,
    purchases           integer,
    purchase_value      numeric(10, 2),
    raw                 jsonb,
    primary key (meta_ad_set_id, date)
);

create index if not exists idx_ad_shirt on ad_insights (shirt_id);

-- Current ad-set status (so recommendations know what's actually running)
create table if not exists meta_adsets (
    meta_ad_set_id   text primary key,
    name             text,
    status           text,            -- configured: ACTIVE | PAUSED | ARCHIVED | DELETED
    effective_status text,            -- actual, incl campaign-level pause
    updated_at       timestamptz default now()
);

-- ---------------------------------------------------------------------------
-- Sync-run log (observability)
-- ---------------------------------------------------------------------------
create table if not exists sync_runs (
    id          bigserial primary key,
    started_at  timestamptz default now(),
    finished_at timestamptz,
    status      text,                            -- running | ok | error
    detail      jsonb
);

-- ---------------------------------------------------------------------------
-- Derived views: per-shirt and per-theme profit + ROAS
-- ---------------------------------------------------------------------------
-- Drop dependents first so columns can be re-shaped (create-or-replace cannot
-- rename/reorder existing view columns).
drop view if exists theme_profit;
drop view if exists shirt_profit;
drop view if exists shirt_revenue;

create or replace view shirt_revenue as
select
    coalesce(li.shirt_id, li.shopify_product_id) as shirt_id,
    sum(li.revenue)                          as revenue,  -- NET kept (after discounts+refunds, incl shipping, excl tax)
    sum(coalesce(li.shipping_revenue, 0))    as shipping_revenue,  -- informational
    sum(coalesce(li.printify_cost, 0))       as printify_cost,
    sum(coalesce(li.fee, 0))                 as fees,
    sum(li.quantity)                         as units
from order_line_items li
group by 1;

create or replace view shirt_ad_spend as
select
    shirt_id,
    sum(spend)          as ad_spend,
    sum(purchases)      as pixel_purchases,
    sum(purchase_value) as pixel_revenue
from ad_insights
where shirt_id is not null
group by 1;

create or replace view shirt_profit as
select
    s.shirt_id,
    s.title,
    s.theme,
    s.funnel_status,
    coalesce(r.revenue, 0)          as revenue,  -- net kept, after discounts+refunds, excl tax
    coalesce(r.shipping_revenue, 0) as shipping_revenue,
    coalesce(r.printify_cost, 0)    as printify_cost,
    coalesce(r.fees, 0)             as fees,
    coalesce(a.ad_spend, 0)         as ad_spend,
    coalesce(r.revenue, 0)
        - coalesce(r.printify_cost, 0)
        - coalesce(r.fees, 0)
        - coalesce(a.ad_spend, 0)   as net_profit,
    case when coalesce(a.ad_spend, 0) > 0
         then round(coalesce(r.revenue, 0) / a.ad_spend, 2) end as roas,
    coalesce(r.units, 0)            as units
from shirts s
left join shirt_revenue r on r.shirt_id = s.shirt_id
left join shirt_ad_spend a on a.shirt_id = s.shirt_id;

create or replace view theme_profit as
select
    coalesce(theme, 'unknown') as theme,
    count(*)                   as shirts,
    sum(revenue)               as revenue,
    sum(printify_cost)         as printify_cost,
    sum(fees)                  as fees,
    sum(ad_spend)              as ad_spend,
    sum(net_profit)            as net_profit,
    case when sum(ad_spend) > 0
         then round(sum(revenue) / sum(ad_spend), 2) end as roas
from shirt_profit
group by 1
order by net_profit desc;
