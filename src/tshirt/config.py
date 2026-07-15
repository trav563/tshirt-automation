"""Loads and validates configuration from the project .env file.

Run `tshirt-check` (or `python -m tshirt.config`) to see which keys are still
missing without printing any secret values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"

# Keys that must be present before the nightly job can run end to end.
REQUIRED_KEYS = (
    "DATABASE_URL",
    "SHOPIFY_STORE_DOMAIN",
    "SHOPIFY_CLIENT_ID",
    "SHOPIFY_CLIENT_SECRET",
    "PRINTIFY_API_TOKEN",
    "META_AD_ACCOUNT_ID",
    "META_SYSTEM_USER_TOKEN",
)

# Helpful but auto-resolvable / has a sane default.
OPTIONAL_KEYS = (
    "PRINTIFY_SHOP_ID",
    "SHOPIFY_API_VERSION",
    "META_APP_ID",
    "META_API_VERSION",
    "SYNC_LOOKBACK_DAYS",
    "DEFAULT_UNIT_COGS",
    "DEFAULT_PRICE",
    "ANTHROPIC_API_KEY",
    "IDEAS_MODEL",
)

_PLACEHOLDERS = {"your-store.myshopify.com"}


@dataclass(frozen=True)
class Config:
    database_url: str
    shopify_store_domain: str
    shopify_client_id: str
    shopify_client_secret: str
    shopify_api_version: str
    printify_api_token: str
    printify_shop_id: str
    meta_app_id: str
    meta_ad_account_id: str
    meta_system_user_token: str
    meta_api_version: str
    sync_lookback_days: int
    default_unit_cogs: float       # fallback print cost when a shirt has no Printify match
    default_price: float           # fallback retail price for break-even math
    anthropic_api_key: str         # optional: only the AI design-ideas panel needs it
    ideas_model: str
    monthly_fixed_costs: float     # Shopify plan + Printify sub + apps (not in any per-order API)

    @classmethod
    def load(cls) -> "Config":
        load_dotenv(ENV_PATH)
        return cls(
            database_url=os.getenv("DATABASE_URL", "").strip(),
            shopify_store_domain=os.getenv("SHOPIFY_STORE_DOMAIN", "").strip(),
            shopify_client_id=os.getenv("SHOPIFY_CLIENT_ID", "").strip(),
            shopify_client_secret=os.getenv("SHOPIFY_CLIENT_SECRET", "").strip(),
            shopify_api_version=os.getenv("SHOPIFY_API_VERSION", "2025-01").strip(),
            printify_api_token=os.getenv("PRINTIFY_API_TOKEN", "").strip(),
            printify_shop_id=os.getenv("PRINTIFY_SHOP_ID", "").strip(),
            meta_app_id=os.getenv("META_APP_ID", "").strip(),
            meta_ad_account_id=os.getenv("META_AD_ACCOUNT_ID", "").strip(),
            meta_system_user_token=os.getenv("META_SYSTEM_USER_TOKEN", "").strip(),
            meta_api_version=os.getenv("META_API_VERSION", "v21.0").strip(),
            sync_lookback_days=int(os.getenv("SYNC_LOOKBACK_DAYS", "60") or "60"),
            default_unit_cogs=float(os.getenv("DEFAULT_UNIT_COGS", "11.00") or "11.00"),
            default_price=float(os.getenv("DEFAULT_PRICE", "24.99") or "24.99"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
            ideas_model=os.getenv("IDEAS_MODEL", "claude-opus-4-8").strip(),
            monthly_fixed_costs=float(os.getenv("MONTHLY_FIXED_COSTS", "0") or "0"),
        )

    def missing_required(self) -> list[str]:
        """Required keys that are empty or still set to a placeholder value."""
        load_dotenv(ENV_PATH)
        out = []
        for key in REQUIRED_KEYS:
            val = (os.getenv(key, "") or "").strip()
            if not val or val in _PLACEHOLDERS:
                out.append(key)
        return out


def main() -> int:
    """`tshirt-check` — report config status without revealing secret values."""
    load_dotenv(ENV_PATH)
    print(f".env: {ENV_PATH}")
    print(f"      ({'found' if ENV_PATH.exists() else 'NOT FOUND'})\n")

    def status(key: str) -> str:
        val = (os.getenv(key, "") or "").strip()
        if not val:
            return "EMPTY"
        if val in _PLACEHOLDERS:
            return "PLACEHOLDER"
        return "set"

    print("Required keys:")
    for key in REQUIRED_KEYS:
        st = status(key)
        flag = "  " if st == "set" else "! "
        print(f"  {flag}{key:<24} {st}")

    print("\nOptional keys:")
    for key in OPTIONAL_KEYS:
        print(f"    {key:<24} {status(key)}")

    missing = Config.load().missing_required()
    print()
    if missing:
        print(f"NOT READY — fill these before running the job: {', '.join(missing)}")
        return 1
    print("READY — all required keys present. You can run tshirt-migrate then tshirt-nightly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
