"""Meta Marketing API client: daily per-ad-set spend + pixel results, and the
ad-set -> shirt_id map derived from each ad's creative url_tags (utm_campaign).

Attribution convention (HANDOFF.md): every ad set's destination URL carries
utm_campaign=shirt_<shopifyProductId>, so shirt_id == that token.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Iterator
from urllib.parse import parse_qs

import requests

from ..config import Config
from . import http

GRAPH = "https://graph.facebook.com"

# Strip the access_token out of any text before it can reach a log or exception.
_TOKEN_RE = re.compile(r"(access_token=)[^&\s\"]+")


def _scrub(text: str) -> str:
    return _TOKEN_RE.sub(r"\1REDACTED", text or "")


class MetaError(RuntimeError):
    pass


def _date_chunks(since: str, until: str, max_days: int = 90):
    """Split [since, until] into <= max_days windows (Meta 500s on long daily ranges)."""
    cur, end = date.fromisoformat(since), date.fromisoformat(until)
    while cur <= end:
        stop = min(cur + timedelta(days=max_days - 1), end)
        yield cur.isoformat(), stop.isoformat()
        cur = stop + timedelta(days=1)

# Action types that count as a purchase, best first.
_PURCHASE_ACTIONS = (
    "purchase",
    "omni_purchase",
    "offsite_conversion.fb_pixel_purchase",
)
_SHIRT_RE = re.compile(r"shirt[_-]?([A-Za-z0-9]+)")


class MetaClient:
    def __init__(self, config: Config):
        self.cfg = config
        self.version = config.meta_api_version
        self.account = config.meta_ad_account_id  # already has the act_ prefix
        self.token = config.meta_system_user_token
        self.session = requests.Session()

    def _paginate(self, path: str, params: dict) -> Iterator[dict]:
        params = {**params, "access_token": self.token, "limit": params.get("limit", 200)}
        url = f"{GRAPH}/{self.version}/{path}"
        while True:
            try:
                resp = http.request(self.session, "GET", url, params=params)
            except requests.RequestException as exc:  # network errors can echo the URL+token
                raise MetaError(_scrub(str(exc))) from None
            if not resp.ok:
                # Never surface the URL/body verbatim: it contains the access token.
                raise MetaError(_scrub(f"Meta API {resp.status_code} on {path}: {resp.text[:300]}"))
            payload = resp.json()
            yield from payload.get("data", [])
            nxt = (payload.get("paging") or {}).get("next")
            if not nxt:
                return
            # `next` is a fully-formed URL; drop our params so we don't double them.
            url, params = nxt, {}

    def iter_adset_insights(self, since: str, until: str) -> Iterator[dict]:
        """Daily insights per ad set, chunked into <=90-day ranges (Meta returns a
        500 on year-long daily requests). since/until are 'YYYY-MM-DD'."""
        for chunk_since, chunk_until in _date_chunks(since, until, 90):
            params = {
                "level": "adset",
                "fields": (
                    "adset_id,adset_name,campaign_id,campaign_name,"
                    "spend,impressions,clicks,actions,action_values"
                ),
                "time_range": f'{{"since":"{chunk_since}","until":"{chunk_until}"}}',
                "time_increment": 1,
            }
            yield from self._paginate(f"{self.account}/insights", params)

    def iter_adsets(self) -> Iterator[dict]:
        """Current ad sets with their live status (ACTIVE / PAUSED / ARCHIVED ...).

        effective_status reflects reality including campaign-level pauses; status
        is the ad set's own configured state.
        """
        yield from self._paginate(
            f"{self.account}/adsets",
            {"fields": "id,name,status,effective_status"},
        )

    def adset_shirt_map(self) -> dict[str, str]:
        """ad_set_id -> shirt_id, parsed from each ad's creative url_tags."""
        mapping: dict[str, str] = {}
        params = {"fields": "adset_id,name,creative{url_tags}"}
        for ad in self._paginate(f"{self.account}/ads", params):
            adset_id = ad.get("adset_id")
            if not adset_id or adset_id in mapping:
                continue
            url_tags = ((ad.get("creative") or {}).get("url_tags")) or ""
            shirt_id = _shirt_from_url_tags(url_tags) or _shirt_from_text(ad.get("name", ""))
            if shirt_id:
                mapping[str(adset_id)] = shirt_id
        return mapping


def _shirt_from_url_tags(url_tags: str) -> str | None:
    if not url_tags:
        return None
    qs = parse_qs(url_tags)
    campaign = (qs.get("utm_campaign") or [""])[0]
    return _shirt_from_text(campaign)


def _shirt_from_text(text: str) -> str | None:
    if not text:
        return None
    m = _SHIRT_RE.search(text)
    return m.group(1) if m else None


def parse_purchases(actions: list[dict] | None) -> int:
    return int(round(_first_action_value(actions)))


def parse_purchase_value(action_values: list[dict] | None) -> float:
    return round(_first_action_value(action_values), 2)


def _first_action_value(items: list[dict] | None) -> float:
    by_type = {a.get("action_type"): a.get("value") for a in (items or [])}
    for action_type in _PURCHASE_ACTIONS:
        if action_type in by_type:
            try:
                return float(by_type[action_type])
            except (TypeError, ValueError):
                return 0.0
    return 0.0
