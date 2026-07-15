"""Postgres connection helper (Supabase)."""

from __future__ import annotations

import psycopg

from ..config import Config


def connect(cfg: Config | None = None) -> psycopg.Connection:
    cfg = cfg or Config.load()
    if not cfg.database_url:
        raise RuntimeError(
            "DATABASE_URL is not set in .env. Get it from Supabase: "
            "Project Settings -> Database -> Connection string (URI)."
        )
    return psycopg.connect(cfg.database_url)
