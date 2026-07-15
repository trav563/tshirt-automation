"""`tshirt-migrate` — apply schema.sql to the Supabase Postgres database."""

from __future__ import annotations

from pathlib import Path

from .connection import connect

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def main() -> int:
    sql = SCHEMA_PATH.read_text()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    print(f"Schema applied from {SCHEMA_PATH.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
