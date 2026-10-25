"""Shopify Admin client.

Auth (changed Jan 1, 2026): mints a 24h token via the OAuth client_credentials
grant, caches it in memory, and auto-refreshes before expiry. Static shpat_
tokens are discontinued.
"""

from __future__ import annotations

import time
from typing import Iterator

import requests

from ..config import Config
from . import http

_PRODUCTS_QUERY = """
query($cursor: String) {
  products(first: 100, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      title
      status
      productType
      tags
      createdAt
      variants(first: 1) { nodes { price } }
    }
  }
}
"""

_ORDERS_QUERY = """
query($cursor: String, $q: String) {
  orders(first: 100, after: $cursor, query: $q, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      name
      createdAt
      currencyCode
      currentSubtotalPriceSet { shopMoney { amount } }
      totalDiscountsSet { shopMoney { amount } }
      totalShippingPriceSet { shopMoney { amount } }
      totalPriceSet { shopMoney { amount } }
      currentTotalTaxSet { shopMoney { amount } }
      totalRefundedSet { shopMoney { amount } }
      netPaymentSet { shopMoney { amount } }
      customerJourneySummary {
        lastVisit { utmParameters { campaign source medium content term } }
      }
      lineItems(first: 50) {
        nodes {
          id
          quantity
          discountedTotalSet { shopMoney { amount } }
          product { id }
          variant { id }
        }
      }
    }
  }
}
"""


_BALANCE_TX_QUERY = """
query($cursor: String) {
  shopifyPaymentsAccount {
    balanceTransactions(first: 100, after: $cursor, reverse: true) {
      pageInfo { hasNextPage endCursor }
      nodes {
        transactionDate
        type
        fee { amount }
        associatedOrder { id }
      }
    }
  }
}
"""


class ShopifyError(RuntimeError):
    pass


class ShopifyClient:
    def __init__(self, config: Config):
        self.cfg = config
        self.domain = config.shopify_store_domain
        self.api_version = config.shopify_api_version
        self.session = requests.Session()
        self._token: str | None = None
        self._token_expiry: float = 0.0

    # --- auth -----------------------------------------------------------------
    def _mint_token(self) -> str:
        url = f"https://{self.domain}/admin/oauth/access_token"
        resp = http.request(
            self.session,
            "POST",
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.cfg.shopify_client_id,
                "client_secret": self.cfg.shopify_client_secret,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        # 5-minute safety buffer before the ~24h expiry.
        self._token_expiry = time.time() + int(data.get("expires_in", 86399)) - 300
        return self._token

    def _get_token(self) -> str:
        if not self._token or time.time() >= self._token_expiry:
            return self._mint_token()
        return self._token

    # --- graphql --------------------------------------------------------------
    def graphql(self, query: str, variables: dict | None = None) -> dict:
        url = f"https://{self.domain}/admin/api/{self.api_version}/graphql.json"
        for attempt in range(4):
            headers = {
                "X-Shopify-Access-Token": self._get_token(),
                "Content-Type": "application/json",
            }
            resp = http.request(
                self.session,
                "POST",
                url,
                json={"query": query, "variables": variables or {}},
                headers=headers,
            )
            resp.raise_for_status()
            payload = resp.json()
            errors = payload.get("errors")
            if errors:
                text = str(errors)
                if "THROTTLED" in text.upper() and attempt < 3:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise ShopifyError(text)
            return payload["data"]
        raise ShopifyError("GraphQL retries exhausted (throttled)")

    # --- domain pulls ---------------------------------------------------------
    def iter_products(self) -> Iterator[dict]:
        cursor = None
        while True:
            data = self.graphql(_PRODUCTS_QUERY, {"cursor": cursor})
            conn = data["products"]
            yield from conn["nodes"]
            if not conn["pageInfo"]["hasNextPage"]:
                return
            cursor = conn["pageInfo"]["endCursor"]

    def iter_balance_transactions(self, since: str | None = None) -> Iterator[dict]:
        """Shopify Payments balance transactions (carry the real processing fee).

        Needs the read_shopify_payments_payouts scope; raises ShopifyError with
        ACCESS_DENIED otherwise. Yields nothing if the store isn't on Shopify
        Payments. Returned newest-first, so we stop once a page is fully older
        than `since`.
        """
        cursor = None
        for _ in range(500):  # hard page cap (safety)
            data = self.graphql(_BALANCE_TX_QUERY, {"cursor": cursor})
            account = data.get("shopifyPaymentsAccount")
            if not account:
                return
            conn = account["balanceTransactions"]
            page_in_window = False
            for node in conn["nodes"]:
                td = node.get("transactionDate")
                if since and td and td[:10] < since:
                    continue
                page_in_window = True
                yield node
            if not conn["pageInfo"]["hasNextPage"]:
                return
            if since and not page_in_window:  # whole page older than window
                return
            cursor = conn["pageInfo"]["endCursor"]

    def iter_orders(self, created_at_min: str | None = None) -> Iterator[dict]:
        """created_at_min: ISO date string, e.g. '2026-04-11'."""
        q = f"created_at:>={created_at_min}" if created_at_min else None
        cursor = None
        while True:
            data = self.graphql(_ORDERS_QUERY, {"cursor": cursor, "q": q})
            conn = data["orders"]
            yield from conn["nodes"]
            if not conn["pageInfo"]["hasNextPage"]:
                return
            cursor = conn["pageInfo"]["endCursor"]


def gid_numeric(gid: str | None) -> str | None:
    """gid://shopify/Product/8123456789 -> '8123456789'."""
    if not gid:
        return None
    return gid.rstrip("/").split("/")[-1]
