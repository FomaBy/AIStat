"""Model pricing, token cost and credits.

Rates live in a versioned data file (``pricing.json``) so they can be edited
or extended without touching code; a second JSON pointed at by
``AISTAT_PRICING_OVERRIDES`` merges on top (add/override a model). Every rate
carries the official vendor source URL and the date it was captured.

Cost of one usage row (four token counts) is a pure function of the row and
the model's rate:

    usd = (input*input_rate + output*output_rate
           + cache_read*cache_read_rate + cache_write*cache_write_rate) / 1e6

``input_tokens`` is the *uncached* remainder (cache reads/writes are counted
separately by Multica), so the four terms simply add up. A model without an
official rate is ``unpriced`` — its cost is ``None`` (never 0), and it is
surfaced in health so it is never silently dropped.

Credits are a separate unit from USD. When a model carries a ``credits`` block
(the OpenAI Codex rate card: credits per 1M tokens for input / cached input /
output), cost_credits is computed directly from it:

    credits = (input*credits.input + cache_read*credits.cache_read
               + output*credits.output) / 1e6

cache_write is not part of the credit rate card. Models without a ``credits``
block fall back to the legacy flat conversion ``usd * AISTAT_CREDITS_PER_USD``.
Claude models carry the mapped Codex-tier credit rates (Fable 5 = GPT-5.6 Sol,
Opus 4.8 = GPT-5.6 Terra) per owner directive FAN-1427.
"""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .db import utcnow_iso

TOKENS_PER_UNIT = 1_000_000


class PricingError(ValueError):
    """pricing.json (or an override) did not match the expected contract."""


@dataclass
class CreditRate:
    """Per-1M-token credit rates for one model (OpenAI Codex rate card unit).

    Covers input / cached input (cache_read) / output only — cache_write is
    not part of the credit rate card.
    """

    input: float
    cache_read: float
    output: float
    source: Optional[str] = None
    captured_at: Optional[str] = None
    notes: Optional[str] = None


@dataclass
class Rate:
    """Per-1M-token rates for one model, with provenance."""

    model: str
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    cache_write_1h: Optional[float] = None
    currency: str = "USD"
    vendor: Optional[str] = None
    source_url: Optional[str] = None
    captured_at: Optional[str] = None
    notes: Optional[str] = None
    unpriced: bool = False
    credits: Optional[CreditRate] = None


@dataclass
class CostResult:
    """Cost of a usage row. ``usd`` is None for an unpriced model."""

    usd: Optional[float]
    priced: bool


_RATE_FIELDS = ("input", "output", "cache_read", "cache_write")
_CREDIT_FIELDS = ("input", "cache_read", "output")


def _parse_credits(entry: Dict[str, Any], model: str,
                   source: str) -> Optional[CreditRate]:
    """Parse an optional ``credits`` block (OpenAI Codex rate card rates)."""
    block = entry.get("credits")
    if block is None:
        return None
    if not isinstance(block, dict):
        raise PricingError(f"{source}: model '{model}' 'credits' is not an object")
    values = {}
    for field in _CREDIT_FIELDS:
        value = block.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise PricingError(
                f"{source}: model '{model}' credits missing numeric '{field}'"
            )
        values[field] = float(value)
    return CreditRate(
        source=block.get("source"),
        captured_at=block.get("captured_at"),
        notes=block.get("notes"),
        **values,
    )


def _parse_models(doc: Dict[str, Any], source: str) -> Dict[str, Rate]:
    models = doc.get("models")
    if not isinstance(models, dict):
        raise PricingError(f"{source}: top-level 'models' object is missing")
    default_currency = doc.get("currency", "USD")
    rates: Dict[str, Rate] = {}
    for model, entry in models.items():
        if not isinstance(entry, dict):
            raise PricingError(f"{source}: model '{model}' is not an object")
        credits = _parse_credits(entry, model, source)
        if entry.get("unpriced"):
            rates[model] = Rate(
                model=model,
                unpriced=True,
                currency=entry.get("currency", default_currency),
                vendor=entry.get("vendor"),
                source_url=entry.get("source_url"),
                captured_at=entry.get("captured_at"),
                notes=entry.get("notes"),
                credits=credits,
            )
            continue
        values = {}
        for field in _RATE_FIELDS:
            value = entry.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise PricingError(
                    f"{source}: model '{model}' missing numeric rate '{field}'"
                )
            values[field] = float(value)
        cw_1h = entry.get("cache_write_1h")
        rates[model] = Rate(
            model=model,
            cache_write_1h=float(cw_1h) if isinstance(cw_1h, (int, float)) else None,
            currency=entry.get("currency", default_currency),
            vendor=entry.get("vendor"),
            source_url=entry.get("source_url"),
            captured_at=entry.get("captured_at"),
            notes=entry.get("notes"),
            credits=credits,
            **values,
        )
    return rates


def _load_file(path: Union[str, Path]) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise PricingError(f"pricing file not found: {path}")
    except json.JSONDecodeError as exc:
        raise PricingError(f"pricing file {path} is not valid JSON: {exc}")


def load_pricing(
    path: Union[str, Path],
    override_path: Optional[Union[str, Path]] = None,
) -> Dict[str, Rate]:
    """Load the base pricing file, then merge an optional override file.

    An override entry for a model replaces the base entry wholesale (so an
    override can also flip a model to ``unpriced`` or price a new one). A
    missing override path is fine; a present-but-broken one raises.
    """
    rates = _parse_models(_load_file(path), str(path))
    if override_path is not None and Path(override_path).exists():
        rates.update(_parse_models(_load_file(override_path), str(override_path)))
    return rates


# -- pure cost computation ---------------------------------------------------


def compute_cost(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    rate: Optional[Rate],
) -> CostResult:
    """USD cost of one usage row. Unpriced / unknown model → cost None."""
    if rate is None or rate.unpriced:
        return CostResult(usd=None, priced=False)
    usd = (
        input_tokens * rate.input
        + output_tokens * rate.output
        + cache_read_tokens * rate.cache_read
        + cache_write_tokens * rate.cache_write
    ) / TOKENS_PER_UNIT
    return CostResult(usd=usd, priced=True)


def compute_credit_cost(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    rate: Optional[Rate],
) -> Optional[float]:
    """Credits for one usage row from the model's Codex credit rate card.

    Returns ``None`` when the model has no ``credits`` block — the caller then
    falls back to the legacy ``usd * credits_per_usd`` conversion. cache_write
    is deliberately excluded: it is not part of the OpenAI Codex credit card.
    """
    if rate is None or rate.credits is None:
        return None
    c = rate.credits
    return (
        input_tokens * c.input
        + output_tokens * c.output
        + cache_read_tokens * c.cache_read
    ) / TOKENS_PER_UNIT


def usd_to_credits(usd: Optional[float], credits_per_usd: float) -> Optional[float]:
    """Convert a USD cost to credits. None stays None (unpriced).

    Legacy fallback used only when a model has no ``credits`` rate card.
    """
    if usd is None:
        return None
    return usd * credits_per_usd


def credits_for_row(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    usd: Optional[float],
    rate: Optional[Rate],
    credits_per_usd: float,
) -> Optional[float]:
    """cost_credits for one row: rate-card credits when available, else the
    legacy flat usd→credits conversion (preserving prior behaviour)."""
    card = compute_credit_cost(input_tokens, output_tokens, cache_read_tokens, rate)
    if card is not None:
        return card
    return usd_to_credits(usd, credits_per_usd)


# -- persistence + recompute -------------------------------------------------


def upsert_model_pricing(conn: sqlite3.Connection, pricing: Dict[str, Rate],
                         loaded_at: Optional[str] = None) -> int:
    """Mirror the loaded rate table into the model_pricing table."""
    loaded_at = loaded_at or utcnow_iso()
    for rate in pricing.values():
        conn.execute(
            """
            INSERT INTO model_pricing (
                model, vendor, currency, input_rate, output_rate,
                cache_read_rate, cache_write_rate, cache_write_1h_rate,
                unpriced, source_url, captured_at, notes, loaded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(model) DO UPDATE SET
                vendor = excluded.vendor,
                currency = excluded.currency,
                input_rate = excluded.input_rate,
                output_rate = excluded.output_rate,
                cache_read_rate = excluded.cache_read_rate,
                cache_write_rate = excluded.cache_write_rate,
                cache_write_1h_rate = excluded.cache_write_1h_rate,
                unpriced = excluded.unpriced,
                source_url = excluded.source_url,
                captured_at = excluded.captured_at,
                notes = excluded.notes,
                loaded_at = excluded.loaded_at
            """,
            (
                rate.model, rate.vendor, rate.currency,
                None if rate.unpriced else rate.input,
                None if rate.unpriced else rate.output,
                None if rate.unpriced else rate.cache_read,
                None if rate.unpriced else rate.cache_write,
                rate.cache_write_1h,
                1 if rate.unpriced else 0,
                rate.source_url, rate.captured_at, rate.notes, loaded_at,
            ),
        )
    return len(pricing)


def recompute_daily_costs(conn: sqlite3.Connection, pricing: Dict[str, Rate],
                          credits_per_usd: float,
                          computed_at: Optional[str] = None) -> int:
    """Recompute cost_usd / cost_credits / cost_priced for every daily_usage row.

    Idempotent: a row's cost depends only on its token counts, the model's
    rate and the configured credit rate, so repeated runs converge.
    """
    computed_at = computed_at or utcnow_iso()
    rows = conn.execute(
        "SELECT rowid, model, input_tokens, output_tokens, "
        "cache_read_tokens, cache_write_tokens FROM daily_usage"
    ).fetchall()
    for row in rows:
        rate = pricing.get(row["model"])
        result = compute_cost(
            row["input_tokens"], row["output_tokens"],
            row["cache_read_tokens"], row["cache_write_tokens"],
            rate,
        )
        credits = credits_for_row(
            row["input_tokens"], row["output_tokens"], row["cache_read_tokens"],
            result.usd, rate, credits_per_usd,
        )
        conn.execute(
            "UPDATE daily_usage SET cost_usd = ?, cost_credits = ?, "
            "cost_priced = ?, cost_computed_at = ? WHERE rowid = ?",
            (
                result.usd,
                credits,
                1 if result.priced else 0,
                computed_at,
                row["rowid"],
            ),
        )
    return len(rows)


def unpriced_models_in_usage(conn: sqlite3.Connection,
                             pricing: Dict[str, Rate]) -> List[str]:
    """Distinct models present in daily_usage that lack an official rate."""
    priced = {m for m, r in pricing.items() if not r.unpriced}
    rows = conn.execute("SELECT DISTINCT model FROM daily_usage ORDER BY model")
    return [r["model"] for r in rows if r["model"] not in priced]
