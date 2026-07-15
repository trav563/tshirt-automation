"""Small shared HTTP helper: one requests.Session call with retry on 429/5xx."""

from __future__ import annotations

import time

import requests


def request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    retries: int = 4,
    backoff: float = 2.0,
    **kwargs,
) -> requests.Response:
    kwargs.setdefault("timeout", 60)
    resp = None
    for attempt in range(retries):
        resp = session.request(method, url, **kwargs)
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
            wait = float(resp.headers.get("Retry-After", backoff * (attempt + 1)))
            time.sleep(wait)
            continue
        return resp
    return resp  # type: ignore[return-value]
