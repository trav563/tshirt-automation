"""AI design-ideas generator.

Asks Claude (via the Anthropic SDK, structured output) to propose ORIGINAL, net-new
shirt concepts. It is given (a) the existing catalog so it never duplicates a design
the store already sells, (b) the per-niche demand signal from Meta ad tests, and
(c) the profit picture. Two modes:
  - "diversify" (default): grow beyond F1 into promising non-F1 niches.
  - "extend": fresh angles inside proven winners (still net-new, never duplicates).
Text only, no image generation.

House rules: no em dash; never duplicate an existing design; flag IP/trademark risk
(F1, NASCAR, team/league/city marks) with a legally-distinct angle.
"""

from __future__ import annotations

from typing import Literal

import psycopg
from pydantic import BaseModel, Field

from .config import Config
from .themes import NICHE_KEYS, classify_theme

SYSTEM = (
    "You are a product strategist for a print-on-demand t-shirt store. You propose "
    "ORIGINAL, net-new shirt design concepts as text (no images). Hard rules:\n"
    "1. Never use an em dash. Use a comma, colon, or period instead.\n"
    "2. NEVER propose a design the store already sells, or a trivial rephrase of one. "
    "You are given the full existing catalog; every idea must be clearly distinct "
    "from all of them in concept AND slogan.\n"
    "3. Flag IP/trademark risk honestly. Real marks (F1, NASCAR, team names, league "
    "logos, city/sports marks) are high risk; use a legally-distinct angle (parody, "
    "generic vibe, original slogan) rather than infringing.\n"
    "4. Slogans must be original and printable on a tee.\n"
    "5. Follow the stated GOAL (diversify vs extend) precisely."
)


class DesignIdea(BaseModel):
    theme: str = Field(description="Niche, e.g. baseball, golf, pickleball, gaming, f1")
    concept: str = Field(description="The visual concept in one sentence")
    slogan: str = Field(description="Original short slogan/text for the shirt")
    rationale: str = Field(description="Why it should work, tied to the demand signal/audience")
    ip_risk: Literal["low", "medium", "high"]


class DesignIdeas(BaseModel):
    ideas: list[DesignIdea]


def _existing_titles(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "select distinct coalesce(title,'') from shirts "
            "where title is not null and title <> '' order by 1"
        )
        return [r[0] for r in cur.fetchall()]


def _niche_signal(conn: psycopg.Connection) -> str:
    """Per-niche Meta ad signal (spend / conversion / pixel ROAS), so ideas target
    niches with real demand and avoid proven dead ends."""
    niche = set(NICHE_KEYS)
    with conn.cursor() as cur:
        cur.execute(
            "select coalesce(campaign_name,'') || ' ' || coalesce(adset_name,'') as name, "
            "sum(spend), sum(clicks), sum(purchases), sum(purchase_value) "
            "from ad_insights group by 1"
        )
        agg: dict[str, list[float]] = {}
        for name, sp, cl, pu, va in cur.fetchall():
            t = classify_theme(None, None, name)
            t = t if t in niche else "other"
            a = agg.setdefault(t, [0.0, 0, 0, 0.0])
            a[0] += float(sp or 0)
            a[1] += int(cl or 0)
            a[2] += int(pu or 0)
            a[3] += float(va or 0)
    lines = ["NICHE DEMAND SIGNAL (Meta ad tests; conversion = purchases/clicks):"]
    for t, (sp, cl, pu, va) in sorted(agg.items(), key=lambda x: -x[1][0]):
        if sp < 50:
            continue
        cvr = 100 * pu / cl if cl else 0.0
        roas = va / sp if sp else 0.0
        verdict = ("real demand" if cvr >= 2.5 else
                   "dead end (clicks, no buyers)" if cvr < 1.2 else "weak")
        lines.append(f"  {t}: spend ${sp:.0f}, conversion {cvr:.1f}%, pixelROAS {roas:.2f} -> {verdict}")
    return "\n".join(lines)


def _profit_context(conn: psycopg.Connection) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "select theme, round(sum(net_profit),2) from shirt_profit "
            "group by theme order by 2 desc nulls last"
        )
        rows = cur.fetchall()
    return "PROFIT BY THEME (reference only, do not copy existing shirts): " + ", ".join(
        f"{t or 'other'} ${p}" for t, p in rows if p is not None
    )


def generate_design_ideas(
    conn: psycopg.Connection, cfg: Config, n: int = 6,
    mode: Literal["diversify", "extend"] = "diversify",
) -> list[DesignIdea]:
    """Returns n original design ideas. Raises if ANTHROPIC_API_KEY is unset."""
    if not cfg.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in .env (needed for AI design ideas).")

    import anthropic  # imported lazily so the rest of the app works without the dep

    titles = _existing_titles(conn)
    catalog = "\n".join(f"  - {t}" for t in titles)
    signal = _niche_signal(conn)
    profit = _profit_context(conn)

    if mode == "diversify":
        goal = (
            "GOAL: diversify BEYOND Formula 1. F1 is ~94% of revenue and the only "
            "consistently profitable theme, but the store wants to grow OTHER themes. "
            "Prioritize niches that show real demand in the signal below; avoid the "
            "dead ends. At most 1 of the ideas may be F1, and only if it is a genuinely "
            "fresh angle unlike anything in the catalog."
        )
    else:
        goal = (
            "GOAL: extend the proven winners with FRESH angles. Stay mostly in "
            "profitable themes, but every concept must be net-new and clearly different "
            "from the existing catalog (new visual idea AND new slogan)."
        )

    prompt = (
        f"{goal}\n\n{signal}\n\n{profit}\n\n"
        f"EXISTING CATALOG (never duplicate or rephrase any of these):\n{catalog}\n\n"
        f"Propose {n} original, specific shirt concepts. For each: the niche, the visual "
        f"concept, an original slogan, why it should work (tie to the demand signal or a "
        f"clear audience), and IP risk. Make them distinct from each other and from the "
        f"catalog. Be specific, not generic."
    )

    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    response = client.messages.parse(
        model=cfg.ideas_model,
        max_tokens=4000,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_format=DesignIdeas,
    )
    return response.parsed_output.ideas
