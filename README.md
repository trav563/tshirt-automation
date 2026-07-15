# T-Shirt Automation — Ads Analysis & Recommendation System

Pulls Shopify (revenue), Printify (true production cost), and Meta (ad spend +
performance) into Supabase Postgres, then recommends **scale / hold / kill /
investigate** per ad set. See `HANDOFF.md` for the full business context.

> Scope: this is the measurement + recommendation layer. The creation/staging
> funnel and ad automation are deferred to a later phase.

## What it does

1. **Nightly ingest** (`tshirt-nightly`) — pulls all three APIs and joins them on
   the attribution convention from `HANDOFF.md`:

   > one shirt = one Shopify product = one Meta ad set = one UTM
   > `shirt_id` = numeric Shopify product id; every ad's URL carries
   > `utm_campaign=shirt_<shirt_id>`.

2. **Recommend** (`tshirt-recommend`) — reads the data and emits per-ad-set
   actions plus an account-level MER check, to terminal and a dated
   `data/recommendations-YYYY-MM-DD.md`.

3. **Dashboard** (`tshirt-dash`, or double-click `Launch Dashboard.command`) —
   an interactive Streamlit dashboard: KPI header, spend-vs-revenue trend, the
   scale/hold/kill action board, per-shirt and per-theme profit, and an optional
   **AI design-ideas** panel that proposes new shirt concepts from your winners.

```
per-shirt net profit = Shopify revenue (after discounts)
           − Printify fulfillment cost (line + shipping)
           − Meta ad spend
           − payment/Shopify fees (~2.9% + $0.30)
```

### How a recommendation is made

Per ad set over the lookback window, using **true Printify cost** when the ad set
is mapped to a shirt (else `DEFAULT_UNIT_COGS`/`DEFAULT_PRICE`):

| Action | When |
|---|---|
| `GATHER` | spend < `$20` — not enough signal yet |
| `INVESTIGATE` | many clicks, 0 sales — landing/offer/price problem |
| `KILL` | spend with no sales, or ROAS below break-even (unprofitable) |
| `HOLD` | profitable but thin (above break-even, below the scale bar) |
| `SCALE` | ROAS ≥ break-even × 1.3 — raise budget |

Break-even ROAS is derived from each shirt's real margin (price − print cost −
fees). Thresholds live at the top of `jobs/recommend.py`.

> **Attribution note:** per-ad-set ROAS uses Meta's pixel attribution (which
> tends to over-report). The account **MER** (Shopify revenue ÷ ad spend) is the
> ground-truth blended check. Per-ad-set numbers become exact once ad URLs carry
> `utm_campaign=shirt_<id>`.

## Layout

```
src/tshirt/
  config.py            load + validate .env (tshirt-check)
  themes.py            tag/title -> niche classifier
  clients/
    shopify.py         client_credentials 24h token + GraphQL orders/products
    printify.py        shops, product cost, order fulfillment cost
    meta.py            daily ad-set insights + ad-set->shirt_id from url_tags
  db/
    schema.sql         tables + profit/ROAS views (idempotent)
    migrate.py         apply schema (tshirt-migrate)
  insights.py          AI design ideas via Claude (claude-opus-4-8)
  jobs/
    nightly.py         pull + upsert + join + ranked report (tshirt-nightly)
    recommend.py       scale/hold/kill per ad set + MER (tshirt-recommend)
  dashboard/
    app.py             Streamlit dashboard
    queries.py         cached DB read helpers
    launch.py          tshirt-dash entry point
deploy/                launchd plist + nightly wrapper
Launch Dashboard.command   double-click to open the dashboard
Refresh Now.command        double-click to pull fresh data now
```

## Setup

1. Fill in `.env` (copy from `.env.example`). Required:
   `DATABASE_URL`, `SHOPIFY_STORE_DOMAIN`, `SHOPIFY_CLIENT_ID`,
   `SHOPIFY_CLIENT_SECRET`, `PRINTIFY_API_TOKEN`, `META_AD_ACCOUNT_ID`,
   `META_SYSTEM_USER_TOKEN`. (`PRINTIFY_SHOP_ID` is auto-resolved if blank.)

2. Install:

   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -e .
   ```

3. Check credentials (prints status, never values):

   ```bash
   tshirt-check
   ```

4. Create the schema, run the sync, then get recommendations:

   ```bash
   tshirt-migrate     # once: create tables/views
   tshirt-nightly     # pull + join
   tshirt-recommend   # scale/hold/kill report + data/recommendations-*.md
   ```

## The dashboard

**Double-click `Launch Dashboard.command`** in Finder (or run `tshirt-dash`). It
opens the dashboard in your browser at `localhost:8501`. A small Terminal window
stays open running the local server; close that window to quit the dashboard.

- **Reload** (sidebar): re-reads the DB. Does *not* pull from Shopify/Meta. Instant.
- **Pull latest data** (sidebar): fetches new orders/ads from all three APIs.
  **Takes ~10-15 minutes** — that is normal, it is not stuck. It runs in the
  background, so you can keep using the dashboard, close the tab, or even quit
  the server; the sidebar shows a live stage-by-stage progress bar while it runs.
  Output is logged to `data/pull.log`. Double-click `Refresh Now.command` to do
  the same thing from Finder.
- Only one sync can run at a time. A second one exits immediately rather than
  running concurrently — two at once corrupt each other's writes (they contend on
  the same upserts and Postgres kills the loser on its 2-minute statement timeout).
- **AI design ideas:** set `ANTHROPIC_API_KEY` in `.env`, then press "Generate
  ideas from my winners". Uses `claude-opus-4-8`. Optional — the rest of the
  dashboard works without it.

## Scheduling (nightly auto-refresh)

> **Currently broken — known issue.** The `launchd` agent is installed and fires
> at 4:10am, but every run fails instantly with exit code 126 and
> `data/nightly.log` fills with:
>
> ```
> /bin/bash: .../deploy/run_nightly.sh: Operation not permitted
> ```
>
> **Cause:** macOS TCC blocks launchd-spawned processes from reading anything
> under `~/Desktop`. It is not a permissions-bit or quarantine problem — `launchd`
> itself is exempt (which is why it can still write the log), but the `bash` it
> spawns is not, so it cannot even open the script. `.env`, `.venv/`, and `src/`
> are all inside the same fence, so pointing the plist directly at the venv
> binary does not help either.
>
> **The fix is to move the project off the Desktop** (e.g. to
> `~/tshirt-automation`), then recreate the venv — shebangs are absolute, so a
> plain `mv` leaves it broken:
>
> ```bash
> mv ~/Desktop/tshirt-automation ~/tshirt-automation
> cd ~/tshirt-automation && rm -rf .venv
> python3 -m venv .venv && .venv/bin/pip install -e .
> ```
>
> Then update the paths in `deploy/com.trendytshirt.nightly.plist` and reinstall
> it. Granting Full Disk Access to `/bin/bash` also works but is a bad trade: it
> gives *every* launchd-spawned shell access to Mail, Messages and Safari, and the
> grant is keyed to the binary's signature, so an OS update silently re-breaks it.
>
> **Until then, use the sidebar "Pull latest data" button** — the data only
> refreshes when you ask it to.

Install (one time):

```bash
cp deploy/com.trendytshirt.nightly.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.trendytshirt.nightly.plist
launchctl start com.trendytshirt.nightly   # optional: run once now to test
```

Logs go to `data/nightly.log`. `launchd` runs missed jobs when the Mac wakes, so
it is more reliable than cron on a laptop. (Edit the paths in the plist if the
project does not live at `/Users/travisames/Desktop/tshirt-automation`.)

## Notes / assumptions

- Fees are estimated at 2.9% + $0.30 per order (Shopify Payments card rate),
  allocated across line items by revenue share. Adjust `FEE_RATE`/`FEE_FIXED`
  in `jobs/nightly.py` if your processor differs.
- `funnel_status` is derived from Shopify product status today
  (ACTIVE→winner, DRAFT→staged, ARCHIVED→killed). When the creation pipeline
  writes an explicit funnel-state metafield, switch to reading that.
- Printify cost is allocated per Shopify order via Printify's `external_id`.
  Raw API payloads are stored in `raw` jsonb columns so joins can be refined
  later without re-pulling.
- `read_all_orders` scope is needed for >60 days of Shopify history.
