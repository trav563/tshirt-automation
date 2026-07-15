"""Map a product's tags / type / title to a niche theme for profit rollups.

The niche list mirrors the collections in HANDOFF.md. First keyword hit wins;
falls back to the Shopify product_type, then "unknown".
"""

from __future__ import annotations

# Order matters: more specific niches first.
NICHE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("f1", ("f1", "formula 1", "formula1", "grand prix", "grandprix",
            "box box", "race week", "lights out", "pit lane", "smooth operator")),
    ("nascar", ("nascar",)),
    ("baseball", ("baseball", "mlb", "fenway", "dugout", "can of corn", "frozen rope")),
    ("golf", ("golf", "pga")),
    ("pickleball", ("pickleball", "dink")),
    ("beer", ("beer", "drinking", "brew", "shower beer")),
    ("cocktail", ("cocktail", "martini", "espresso martini", "old fashioned")),
    ("gaming", ("fallout", "wasteland", "gamer", "arcade", "retro gamer")),
    ("nyc", ("nyc", "new york", "brooklyn", "manhattan", "bronx")),
    ("country", ("country concert", "country music", "nashville")),
    ("spooky", ("spooky", "halloween", "ghost", "witch")),
    ("beach", ("beach", "summer", "surf", "bachelorette")),
    ("funny", ("funny", "humor", "sarcastic", "meme")),
    ("seasonal", ("seasonal", "christmas", "holiday", "valentine", "1776")),
]

# Canonical niche labels (everything else rolls up to "other" in niche analysis).
NICHE_KEYS = [theme for theme, _ in NICHE_KEYWORDS]


def classify_theme(tags: list[str] | None, product_type: str | None, title: str | None) -> str:
    haystack = " ".join(
        [
            " ".join(tags or []),
            product_type or "",
            title or "",
        ]
    ).lower()
    for theme, keywords in NICHE_KEYWORDS:
        if any(kw in haystack for kw in keywords):
            return theme
    if product_type:
        return product_type.strip().lower()
    return "unknown"
