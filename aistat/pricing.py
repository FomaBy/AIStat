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
import math
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .db import utcnow_iso

TOKENS_PER_UNIT = 1_000_000


class PricingError(ValueError):
    """pricing.json (or an override) did not match the expected contract."""


class CreditRate:
    """Per-1M-token credit rates for one model (OpenAI Codex rate card unit).

    Covers input / cached input (cache_read) / output only — cache_write is
    not part of the credit rate card.
    """

    def __init__(
        self,
        input: float,
        cache_read: float,
        output: float,
        source: Optional[str] = None,
        captured_at: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> None:
        self.input = input
        self.cache_read = cache_read
        self.output = output
        self.source = source
        self.captured_at = captured_at
        self.notes = notes


class Rate:
    """Per-1M-token rates for one model, with provenance."""

    def __init__(
        self,
        model: str,
        input: float = 0.0,
        output: float = 0.0,
        cache_read: float = 0.0,
        cache_write: float = 0.0,
        cache_write_1h: Optional[float] = None,
        currency: str = "USD",
        vendor: Optional[str] = None,
        source_url: Optional[str] = None,
        captured_at: Optional[str] = None,
        notes: Optional[str] = None,
        unpriced: bool = False,
        credits: Optional[CreditRate] = None,
        effective_from: Optional[str] = None,
    ) -> None:
        self.model = model
        self.input = input
        self.output = output
        self.cache_read = cache_read
        self.cache_write = cache_write
        self.cache_write_1h = cache_write_1h
        self.currency = currency
        self.vendor = vendor
        self.source_url = source_url
        self.captured_at = captured_at
        self.notes = notes
        self.unpriced = unpriced
        self.credits = credits
        self.effective_from = effective_from


class PriceCatalog(dict):
    """Current rates plus their immutable effective-dated revisions."""

    def __init__(self, rates, revisions):
        super().__init__(rates)
        self.revisions = revisions


class CostResult:
    """Cost of a usage row. ``usd`` is None for an unpriced model."""

    def __init__(self, usd: Optional[float], priced: bool) -> None:
        self.usd = usd
        self.priced = priced


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


def _parse_rate(entry: Dict[str, Any], model: str, source: str,
                default_currency: str) -> Rate:
    if not isinstance(entry, dict):
        raise PricingError(f"{source}: model '{model}' is not an object")
    credits = _parse_credits(entry, model, source)
    if entry.get("unpriced"):
        return Rate(model=model, unpriced=True,
                    currency=entry.get("currency", default_currency),
                    vendor=entry.get("vendor"), source_url=entry.get("source_url"),
                    captured_at=entry.get("captured_at"), notes=entry.get("notes"),
                    credits=credits, effective_from=entry.get("effective_from"))
    values = {}
    for field in _RATE_FIELDS:
        value = entry.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise PricingError(f"{source}: model '{model}' missing numeric rate '{field}'")
        values[field] = float(value)
    cw_1h = entry.get("cache_write_1h")
    return Rate(model=model,
                cache_write_1h=float(cw_1h) if isinstance(cw_1h, (int, float)) else None,
                currency=entry.get("currency", default_currency),
                vendor=entry.get("vendor"), source_url=entry.get("source_url"),
                captured_at=entry.get("captured_at"), notes=entry.get("notes"),
                credits=credits, effective_from=entry.get("effective_from"), **values)


def _parse_models(doc: Dict[str, Any], source: str) -> PriceCatalog:
    models = doc.get("models")
    if not isinstance(models, dict):
        raise PricingError(f"{source}: top-level 'models' object is missing")
    default_currency = doc.get("currency", "USD")
    rates: Dict[str, Rate] = {}
    revisions: Dict[str, List[Rate]] = {}
    for model, entry in models.items():
        entries = entry if isinstance(entry, list) else [entry]
        parsed = [_parse_rate(item, model, source, default_currency) for item in entries]
        if len(parsed) > 1 and any(not rate.effective_from for rate in parsed):
            raise PricingError(f"{source}: dated model '{model}' needs effective_from")
        parsed.sort(key=lambda rate: rate.effective_from or "0001-01-01")
        revisions[model] = parsed
        rates[model] = parsed[-1]
    return PriceCatalog(rates, revisions)


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
) -> PriceCatalog:
    """Load the base pricing file, then merge an optional override file.

    An override entry for a model replaces the base entry wholesale (so an
    override can also flip a model to ``unpriced`` or price a new one). A
    missing override path is fine; a present-but-broken one raises.
    """
    rates = _parse_models(_load_file(path), str(path))
    if override_path is not None and Path(override_path).exists():
        override = _parse_models(_load_file(override_path), str(override_path))
        rates.update(override)
        rates.revisions.update(override.revisions)
    return rates


def effective_rate(pricing: Dict[str, Rate], model: str, usage_date: str) -> Optional[Rate]:
    """Select the last published rate effective on a UTC usage date."""
    revisions = getattr(pricing, "revisions", {}).get(model)
    if not revisions:
        return pricing.get(model)
    candidates = [rate for rate in revisions
                  if not rate.effective_from or rate.effective_from <= usage_date]
    return candidates[-1] if candidates else None


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
    for model, revisions in getattr(pricing, "revisions", {}).items():
        for rate in revisions:
            if not rate.effective_from:
                continue
            # Price history is deliberately insert-only. Correcting a published
            # rate requires a new effective date, never a silent rewrite.
            conn.execute(
                """
                INSERT OR IGNORE INTO model_price_history (
                    model, effective_from, vendor, currency, input_rate,
                    output_rate, cache_read_rate, cache_write_rate,
                    cache_write_1h_rate, credit_input_rate,
                    credit_cache_read_rate, credit_output_rate, unpriced,
                    source_url, captured_at, loaded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (rate.model, rate.effective_from, rate.vendor, rate.currency,
                 None if rate.unpriced else rate.input,
                 None if rate.unpriced else rate.output,
                 None if rate.unpriced else rate.cache_read,
                 None if rate.unpriced else rate.cache_write,
                 rate.cache_write_1h,
                 rate.credits.input if rate.credits else None,
                 rate.credits.cache_read if rate.credits else None,
                 rate.credits.output if rate.credits else None,
                 1 if rate.unpriced else 0, rate.source_url, rate.captured_at,
                 loaded_at),
            )
    return len(pricing)


def _historical_rate(conn: sqlite3.Connection, pricing: Dict[str, Rate],
                     model: str, usage_date: str) -> Optional[Rate]:
    """Use persisted history when present, otherwise the supplied catalog."""
    row = conn.execute(
        "SELECT * FROM model_price_history WHERE model = ? "
        "AND effective_from <= ? ORDER BY effective_from DESC LIMIT 1",
        (model, usage_date),
    ).fetchone()
    if row is None:
        # A catalog's first confirmed historical revision cannot make earlier
        # already-priced usage silently become unpriced. Until a matching
        # revision exists, retain the catalog's compatibility rate.
        return effective_rate(pricing, model, usage_date) or pricing.get(model)
    credits = None
    if row["credit_input_rate"] is not None:
        credits = CreditRate(row["credit_input_rate"], row["credit_cache_read_rate"],
                             row["credit_output_rate"])
    return Rate(model=model, input=row["input_rate"] or 0.0,
                output=row["output_rate"] or 0.0,
                cache_read=row["cache_read_rate"] or 0.0,
                cache_write=row["cache_write_rate"] or 0.0,
                cache_write_1h=row["cache_write_1h_rate"], credits=credits,
                unpriced=bool(row["unpriced"]), vendor=row["vendor"],
                currency=row["currency"] or "USD", source_url=row["source_url"],
                captured_at=row["captured_at"], effective_from=row["effective_from"])


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
        "cache_read_tokens, cache_write_tokens, date FROM daily_usage"
    ).fetchall()
    for row in rows:
        rate = _historical_rate(conn, pricing, row["model"], row["date"])
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
            "cost_priced = ?, cost_computed_at = ?, rate_effective_from = ? WHERE rowid = ?",
            (
                result.usd,
                credits,
                1 if result.priced else 0,
                computed_at,
                rate.effective_from if rate else None,
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


_BILLING_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_BILLING_PERIOD = re.compile(r"^\d{4}-\d{2}$")


def reconcile_billing_actual(conn: sqlite3.Connection, provider: str, period: str,
                             calculated_usd: float, actual_usd: float,
                             threshold: float = 0.05) -> Dict[str, Any]:
    """Store sanitized provider totals and emit one diagnostic per variance.

    Callers retain the export/invoice outside the repository; this table holds
    only period totals required to explain a cost mismatch.
    """
    if not _BILLING_PROVIDER.fullmatch(provider) or not _BILLING_PERIOD.fullmatch(period):
        raise PricingError("provider and period must use canonical identifiers")
    values = (calculated_usd, actual_usd, threshold)
    if any(not isinstance(value, (int, float)) or not math.isfinite(value)
           for value in values) or calculated_usd < 0 or actual_usd < 0 or threshold < 0:
        raise PricingError("billing totals and threshold must be finite non-negative numbers")
    variance = abs(calculated_usd - actual_usd) / actual_usd if actual_usd else (
        0.0 if calculated_usd == 0 else 1.0
    )
    over = variance > threshold
    previous = conn.execute(
        "SELECT over_threshold, diagnostic_emitted_at FROM billing_reconciliation "
        "WHERE provider = ? AND period = ?", (provider, period)
    ).fetchone()
    emitted = bool(over and (previous is None or not previous["over_threshold"]))
    conn.execute(
        """
        INSERT INTO billing_reconciliation (
            provider, period, calculated_usd, actual_usd, variance_ratio,
            over_threshold, diagnostic_emitted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(provider, period) DO UPDATE SET
            calculated_usd = excluded.calculated_usd,
            actual_usd = excluded.actual_usd,
            variance_ratio = excluded.variance_ratio,
            over_threshold = excluded.over_threshold,
            diagnostic_emitted_at = CASE
                WHEN excluded.over_threshold = 1
                 AND billing_reconciliation.over_threshold = 0 THEN excluded.diagnostic_emitted_at
                WHEN excluded.over_threshold = 0 THEN NULL
                ELSE billing_reconciliation.diagnostic_emitted_at END
        """,
        (provider, period, float(calculated_usd), float(actual_usd), variance,
         1 if over else 0, utcnow_iso() if emitted else None),
    )
    return {"provider": provider, "period": period, "variance_ratio": variance,
            "over_threshold": over, "diagnostic_emitted": emitted}
