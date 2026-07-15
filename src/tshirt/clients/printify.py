"""Printify client: shops, products (variant cost), and orders (fulfillment cost).

Costs come back in cents; helpers convert to dollars. Products carry an
`external.id` that is the Shopify product id, which is how we join Printify cost
to the Shopify catalog.
"""

from __future__ import annotations

from typing import Iterator

import requests

from ..config import Config
from . import http

BASE = "https://api.printify.com/v1"


class PrintifyClient:
    def __init__(self, config: Config):
        self.cfg = config
        self._shop_id = config.printify_shop_id or None
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {config.printify_api_token}",
                "User-Agent": "tshirt-automation",
            }
        )

    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = http.request(self.session, "GET", f"{BASE}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_shops(self) -> list[dict]:
        return self._get("/shops.json")

    def resolve_shop_id(self) -> str:
        if self._shop_id:
            return self._shop_id
        shops = self.get_shops()
        if not shops:
            raise RuntimeError("Printify returned no shops for this token.")
        self._shop_id = str(shops[0]["id"])
        return self._shop_id

    def _iter_paginated(self, path: str) -> Iterator[dict]:
        page = 1
        while True:
            payload = self._get(path, params={"page": page, "limit": 50})  # Printify caps limit at 50
            data = payload.get("data", [])
            if not data:
                return
            yield from data
            last_page = payload.get("last_page")
            if last_page is not None and page >= last_page:
                return
            page += 1

    def iter_products(self) -> Iterator[dict]:
        shop = self.resolve_shop_id()
        yield from self._iter_paginated(f"/shops/{shop}/products.json")

    def iter_orders(self) -> Iterator[dict]:
        shop = self.resolve_shop_id()
        yield from self._iter_paginated(f"/shops/{shop}/orders.json")


def cents_to_dollars(value) -> float:
    try:
        return round(int(value) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


def product_base_cost(product: dict) -> float | None:
    """Representative base fulfillment cost = cheapest enabled variant cost."""
    variants = product.get("variants") or []
    costs = [v.get("cost") for v in variants if v.get("is_enabled") and v.get("cost")]
    if not costs:
        costs = [v.get("cost") for v in variants if v.get("cost")]
    if not costs:
        return None
    return cents_to_dollars(min(costs))


def order_costs(order: dict) -> tuple[float, float]:
    """(line_items_cost, shipping_cost) in dollars for a Printify order."""
    line_cost = sum(int(li.get("cost") or 0) for li in order.get("line_items", []))
    shipping = int(order.get("total_shipping") or 0)
    return cents_to_dollars(line_cost), cents_to_dollars(shipping)
