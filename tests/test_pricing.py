"""Tests for the pricing / cost / credits module (stage 2)."""

import json
import sqlite3
from pathlib import Path

import pytest

from aistat import pricing
from aistat.db import utcnow_iso
from aistat.health import snapshot

REPO_ROOT = Path(__file__).resolve().parent.parent
PRICING_JSON = REPO_ROOT / "pricing.json"


# -- pure cost computation ---------------------------------------------------


def _opus_rate():
    return pricing.Rate(
        model="claude-opus-4-8", input=5.0, output=25.0,
        cache_read=0.5, cache_write=6.25,
    )


def test_compute_cost_all_four_token_kinds():
    # (1000*5 + 2000*25 + 1_000_000*0.5 + 1000*6.25) / 1e6
    result = pricing.compute_cost(1000, 2000, 1_000_000, 1000, _opus_rate())
    assert result.priced is True
    assert result.usd == pytest.approx(0.56125)


def test_compute_cost_counts_cache_read_and_write_separately():
    read_only = pricing.compute_cost(0, 0, 1_000_000, 0, _opus_rate())
    write_only = pricing.compute_cost(0, 0, 0, 1_000_000, _opus_rate())
    assert read_only.usd == pytest.approx(0.5)      # 0.1x input
    assert write_only.usd == pytest.approx(6.25)    # 1.25x input


def test_compute_cost_unpriced_model_is_none_not_zero():
    result = pricing.compute_cost(1000, 2000, 3000, 4000, None)
    assert result.priced is False
    assert result.usd is None

    flagged = pricing.Rate(model="internal-x", unpriced=True)
    result = pricing.compute_cost(1000, 2000, 3000, 4000, flagged)
    assert result.priced is False
    assert result.usd is None


def test_usd_to_credits():
    assert pricing.usd_to_credits(2.0, 1.0) == pytest.approx(2.0)
    assert pricing.usd_to_credits(2.0, 2.5) == pytest.approx(5.0)
    assert pricing.usd_to_credits(None, 3.0) is None


# -- credits: OpenAI Codex rate card -----------------------------------------


def _sol_credit_rate():
    # GPT-5.6 Sol credit rate card (credits per 1M tokens).
    return pricing.Rate(
        model="gpt-5.6-sol",
        credits=pricing.CreditRate(input=125.0, cache_read=12.5, output=750.0),
    )


def test_compute_credit_cost_uses_rate_card():
    # 1M of each priced token kind on Sol: 125 + 12.5 + 750.
    credits = pricing.compute_credit_cost(
        1_000_000, 1_000_000, 1_000_000, _sol_credit_rate()
    )
    assert credits == pytest.approx(887.5)


def test_compute_credit_cost_ignores_cache_write():
    # compute_credit_cost has no cache_write parameter; cache_write never bills
    # credits. Same in/out/cache_read must give the same credits regardless.
    a = pricing.compute_credit_cost(1000, 2000, 500, _sol_credit_rate())
    b = pricing.compute_credit_cost(1000, 2000, 500, _sol_credit_rate())
    assert a == b == pytest.approx((1000 * 125 + 2000 * 750 + 500 * 12.5) / 1e6)


def test_compute_credit_cost_none_without_rate_card():
    # A model with no credits block, and an unknown model, both return None so
    # the caller falls back to the usd→credits conversion.
    no_credits = pricing.Rate(model="x", input=1.0, output=5.0,
                              cache_read=0.1, cache_write=1.25)
    assert pricing.compute_credit_cost(1000, 2000, 3000, no_credits) is None
    assert pricing.compute_credit_cost(1000, 2000, 3000, None) is None


def test_credits_for_row_prefers_rate_card_over_conversion():
    # With a credit card, the flat usd*credits_per_usd is NOT used.
    from_card = pricing.credits_for_row(
        1_000_000, 0, 0, usd=999.0, rate=_sol_credit_rate(), credits_per_usd=2.0
    )
    assert from_card == pytest.approx(125.0)          # 1M input * 125
    # Without a credit card, it falls back to usd * credits_per_usd.
    fallback = pricing.credits_for_row(
        1000, 2000, 0, usd=0.011,
        rate=pricing.Rate(model="x", input=1.0, output=5.0,
                          cache_read=0.1, cache_write=1.25),
        credits_per_usd=2.0,
    )
    assert fallback == pytest.approx(0.022)           # 0.011 * 2.0


def test_repo_pricing_json_maps_claude_to_codex_credit_tiers():
    # Owner directive FAN-1427: Fable 5 = GPT-5.6 Sol, Opus 4.8 = GPT-5.6 Terra.
    rates = pricing.load_pricing(PRICING_JSON)
    fable, sol = rates["claude-fable-5"].credits, rates["gpt-5.6-sol"].credits
    opus, terra = rates["claude-opus-4-8"].credits, rates["gpt-5.6-terra"].credits
    assert (fable.input, fable.cache_read, fable.output) == (125.0, 12.5, 750.0)
    assert (fable.input, fable.cache_read, fable.output) == \
           (sol.input, sol.cache_read, sol.output)
    assert (opus.input, opus.cache_read, opus.output) == (50.0, 5.0, 300.0)
    assert (opus.input, opus.cache_read, opus.output) == \
           (terra.input, terra.cache_read, terra.output)


def test_opus_5_is_a_distinct_id_with_opus_4_8_rates_and_credits():
    rates = pricing.load_pricing(PRICING_JSON)
    opus_4_8, opus_5 = rates["claude-opus-4-8"], rates["claude-opus-5"]
    assert opus_4_8.model != opus_5.model
    assert (opus_5.input, opus_5.output, opus_5.cache_read,
            opus_5.cache_write, opus_5.cache_write_1h) == \
        (opus_4_8.input, opus_4_8.output, opus_4_8.cache_read,
         opus_4_8.cache_write, opus_4_8.cache_write_1h)
    assert (opus_5.credits.input, opus_5.credits.cache_read,
            opus_5.credits.output) == \
        (opus_4_8.credits.input, opus_4_8.credits.cache_read,
         opus_4_8.credits.output) == (50.0, 5.0, 300.0)

    # 1M input + cache read + output + cache write: cache write is USD-only.
    for rate in (opus_4_8, opus_5):
        assert pricing.compute_cost(
            1_000_000, 1_000_000, 1_000_000, 1_000_000, rate
        ).usd == pytest.approx(36.75)
        assert pricing.compute_credit_cost(
            1_000_000, 1_000_000, 1_000_000, rate
        ) == pytest.approx(355.0)


def test_credits_block_must_be_numeric(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"models": {"m": {
        "input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25,
        "credits": {"input": 1.0, "cache_read": 0.1},  # missing output
    }}}), encoding="utf-8")
    with pytest.raises(pricing.PricingError):
        pricing.load_pricing(bad)


# -- pricing.json loading ----------------------------------------------------


def test_load_repo_pricing_json_has_official_rates_and_sources():
    rates = pricing.load_pricing(PRICING_JSON)
    for model in ("claude-opus-4-8", "claude-fable-5",
                  "claude-haiku-4-5-20251001", "gpt-5.6-sol", "gpt-5.6-terra",
                  "gpt-5.6-luna"):
        assert model in rates, model
        rate = rates[model]
        assert not rate.unpriced
        assert rate.source_url and rate.source_url.startswith("https://")
        assert rate.captured_at  # every rate records when it was taken

    opus = rates["claude-opus-4-8"]
    assert (opus.input, opus.output, opus.cache_read, opus.cache_write) == (5.0, 25.0, 0.5, 6.25)
    sol = rates["gpt-5.6-sol"]
    assert (sol.input, sol.output, sol.cache_read) == (5.0, 30.0, 0.5)


def test_repo_gpt_5_6_standard_rates_and_credits_are_current():
    rates = pricing.load_pricing(PRICING_JSON)
    expected = {
        "gpt-5.6-luna": ((0.2, 1.2, 0.02, 0.25), (5.0, 0.5, 30.0)),
        "gpt-5.6-terra": ((2.0, 12.0, 0.2, 2.5), (50.0, 5.0, 300.0)),
        "gpt-5.6-sol": ((5.0, 30.0, 0.5, 6.25), (125.0, 12.5, 750.0)),
    }
    for model, (usd, credits) in expected.items():
        rate = rates[model]
        assert (rate.input, rate.output, rate.cache_read, rate.cache_write) == usd
        assert (rate.credits.input, rate.credits.cache_read, rate.credits.output) == credits
        assert rate.captured_at == rate.credits.captured_at == "2026-07-30"


def test_repo_pricing_has_an_effective_date_for_each_confirmed_rate():
    rates = pricing.load_pricing(PRICING_JSON)
    assert all(rate.effective_from for rate in rates.values())


def test_repo_sonnet_rates_match_official_table():
    # FAN-2161: Sonnet rows from the official Anthropic pricing table.
    # Sonnet 5 carries introductory pricing through 2026-08-31 ($3/$15 after);
    # the other Sonnet models are at standard rates. No Sonnet model has a
    # credits block (no owner directive maps Sonnet to a Codex credit tier),
    # so cost_credits falls back to usd*AISTAT_CREDITS_PER_USD.
    rates = pricing.load_pricing(PRICING_JSON)
    expected = {
        "claude-sonnet-5": (2.0, 10.0, 0.2, 2.5, 4.0),
        "claude-sonnet-4-6": (3.0, 15.0, 0.3, 3.75, 6.0),
        "claude-sonnet-4-5-20250929": (3.0, 15.0, 0.3, 3.75, 6.0),
        "claude-sonnet-4-20250514": (3.0, 15.0, 0.3, 3.75, 6.0),
    }
    for model, usd in expected.items():
        rate = rates[model]
        assert not rate.unpriced
        assert (rate.input, rate.output, rate.cache_read,
                rate.cache_write, rate.cache_write_1h) == usd, model
        assert rate.credits is None, model
        assert rate.source_url.startswith("https://platform.claude.com/")
        assert rate.captured_at == "2026-08-05"


def test_health_mirrors_current_gpt_5_6_rates(conn):
    rates = pricing.load_pricing(PRICING_JSON)
    pricing.upsert_model_pricing(conn, rates)
    health_rates = {rate["model"]: rate for rate in snapshot(conn)["pricing"]["rates"]}
    for model, expected in {
        "gpt-5.6-luna": (0.2, 1.2, 0.02, 0.25),
        "gpt-5.6-terra": (2.0, 12.0, 0.2, 2.5),
        "gpt-5.6-sol": (5.0, 30.0, 0.5, 6.25),
    }.items():
        rate = health_rates[model]
        assert (rate["input_rate"], rate["output_rate"],
                rate["cache_read_rate"], rate["cache_write_rate"]) == expected
        assert rate["captured_at"] == "2026-07-30"


def test_load_pricing_override_extends_and_replaces(tmp_path):
    override = tmp_path / "override.json"
    override.write_text(json.dumps({
        "models": {
            # re-rate an existing model
            "claude-opus-4-8": {"input": 4.0, "output": 20.0,
                                "cache_read": 0.4, "cache_write": 5.0,
                                "source_url": "https://example/override",
                                "captured_at": "2026-07-15"},
            # add a brand-new one
            "gpt-5.6-luna": {"input": 1.0, "output": 6.0,
                             "cache_read": 0.1, "cache_write": 1.25,
                             "source_url": "https://developers.openai.com/api/docs/pricing",
                             "captured_at": "2026-07-15"},
        }
    }), encoding="utf-8")

    rates = pricing.load_pricing(PRICING_JSON, override_path=override)
    assert rates["claude-opus-4-8"].input == 4.0        # overridden
    assert rates["gpt-5.6-luna"].output == 6.0          # added
    assert rates["claude-fable-5"].input == 10.0        # untouched base entry


def test_load_pricing_missing_file_raises():
    with pytest.raises(pricing.PricingError):
        pricing.load_pricing(REPO_ROOT / "does-not-exist.json")


def test_load_pricing_missing_rate_raises(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"models": {"m": {"input": 1.0}}}), encoding="utf-8")
    with pytest.raises(pricing.PricingError):
        pricing.load_pricing(bad)


def test_load_pricing_unpriced_entry(tmp_path):
    doc = tmp_path / "p.json"
    doc.write_text(json.dumps({
        "models": {"secret-model": {"unpriced": True, "notes": "internal"}}
    }), encoding="utf-8")
    rates = pricing.load_pricing(doc)
    assert rates["secret-model"].unpriced is True


# -- persistence + recompute over a fixture DB -------------------------------


def _insert_usage(conn, runtime_id, model, date, inp, outp, cr, cw):
    conn.execute(
        "INSERT INTO daily_usage (runtime_id, model, date, input_tokens, "
        "output_tokens, cache_read_tokens, cache_write_tokens, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (runtime_id, model, date, inp, outp, cr, cw, utcnow_iso()),
    )


def test_recompute_daily_costs_prices_known_and_flags_unknown(conn):
    _insert_usage(conn, "rt1", "claude-opus-4-8", "2026-07-14", 1000, 2000, 1_000_000, 1000)
    _insert_usage(conn, "rt2", "totally-internal-model", "2026-07-14", 500, 500, 500, 0)
    # haiku has a USD rate but no credit rate card -> credits fall back to usd*mult.
    _insert_usage(conn, "rt3", "claude-haiku-4-5-20251001", "2026-07-14", 1000, 2000, 0, 0)

    rates = pricing.load_pricing(PRICING_JSON)
    pricing.upsert_model_pricing(conn, rates)
    n = pricing.recompute_daily_costs(conn, rates, credits_per_usd=2.0)
    assert n == 3

    opus = conn.execute(
        "SELECT cost_usd, cost_credits, cost_priced FROM daily_usage WHERE model=?",
        ("claude-opus-4-8",),
    ).fetchone()
    assert opus["cost_priced"] == 1
    assert opus["cost_usd"] == pytest.approx(0.56125)
    # Credits now come from the GPT-5.6 Terra rate card, NOT usd*mult:
    # (1000*50 + 2000*300 + 1_000_000*5) / 1e6. cache_write excluded.
    assert opus["cost_credits"] == pytest.approx(5.65)

    # Model without a credit card keeps the legacy usd*credits_per_usd behaviour.
    haiku = conn.execute(
        "SELECT cost_usd, cost_credits FROM daily_usage WHERE model=?",
        ("claude-haiku-4-5-20251001",),
    ).fetchone()
    assert haiku["cost_usd"] == pytest.approx(0.011)      # (1000*1 + 2000*5)/1e6
    assert haiku["cost_credits"] == pytest.approx(0.022)  # usd * 2.0

    unknown = conn.execute(
        "SELECT cost_usd, cost_credits, cost_priced FROM daily_usage WHERE model=?",
        ("totally-internal-model",),
    ).fetchone()
    assert unknown["cost_priced"] == 0
    assert unknown["cost_usd"] is None
    assert unknown["cost_credits"] is None


def test_mixed_opus_day_keeps_two_priced_rows(conn):
    """One runtime/day may contain both exact Opus IDs without an overwrite."""
    for model in ("claude-opus-4-8", "claude-opus-5"):
        _insert_usage(
            conn, "fb4bfde9-ea2a-4dba-8a4f-bafd8d7c9188", model,
            "2026-07-24", 1_000_000, 1_000_000, 1_000_000, 1_000_000,
        )
    rates = pricing.load_pricing(PRICING_JSON)
    pricing.recompute_daily_costs(conn, rates, credits_per_usd=1.0)

    rows = conn.execute(
        """
        SELECT model, cost_usd, cost_credits, cost_priced FROM daily_usage
        WHERE runtime_id = ? AND date = ? ORDER BY model
        """,
        ("fb4bfde9-ea2a-4dba-8a4f-bafd8d7c9188", "2026-07-24"),
    ).fetchall()
    assert [row["model"] for row in rows] == ["claude-opus-4-8", "claude-opus-5"]
    assert all(row["cost_priced"] == 1 for row in rows)
    assert all(row["cost_usd"] == pytest.approx(36.75) for row in rows)
    assert all(row["cost_credits"] == pytest.approx(355.0) for row in rows)
    assert pricing.unpriced_models_in_usage(conn, rates) == []


def test_recompute_current_luna_cost_is_idempotent(conn):
    _insert_usage(
        conn, "rt1", "gpt-5.6-luna", "2026-07-14",
        1_000_000, 1_000_000, 1_000_000, 1_000_000,
    )
    rates = pricing.load_pricing(PRICING_JSON)
    pricing.recompute_daily_costs(conn, rates, credits_per_usd=1.0)
    first = conn.execute(
        "SELECT cost_usd, cost_credits, cost_priced FROM daily_usage"
    ).fetchone()
    pricing.recompute_daily_costs(conn, rates, credits_per_usd=1.0)
    second = conn.execute(
        "SELECT cost_usd, cost_credits, cost_priced FROM daily_usage"
    ).fetchone()
    assert tuple(first) == tuple(second)
    assert first["cost_usd"] == pytest.approx(1.67)
    assert first["cost_credits"] == pytest.approx(35.5)
    assert first["cost_priced"] == 1


def test_effective_rate_keeps_prior_usage_on_its_historical_price(conn, tmp_path):
    """A later catalog revision must not reprice a closed usage day."""
    catalog = tmp_path / "dated-pricing.json"
    catalog.write_text(json.dumps({"models": {"vendor/m": [
        {"effective_from": "2026-01-01", "input": 1, "output": 1,
         "cache_read": 1, "cache_write": 1, "source_url": "https://vendor/pricing"},
        {"effective_from": "2026-02-01", "input": 2, "output": 2,
         "cache_read": 2, "cache_write": 2, "source_url": "https://vendor/pricing"},
    ]}}), encoding="utf-8")
    _insert_usage(conn, "rt", "vendor/m", "2026-01-31", 1_000_000, 0, 0, 0)
    _insert_usage(conn, "rt", "vendor/m", "2026-02-01", 1_000_000, 0, 0, 0)

    rates = pricing.load_pricing(catalog)
    pricing.upsert_model_pricing(conn, rates)
    pricing.recompute_daily_costs(conn, rates, credits_per_usd=1.0)
    first = conn.execute(
        "SELECT date, cost_usd, rate_effective_from FROM daily_usage ORDER BY date"
    ).fetchall()
    assert [(r["date"], r["cost_usd"], r["rate_effective_from"]) for r in first] == [
        ("2026-01-31", 1.0, "2026-01-01"),
        ("2026-02-01", 2.0, "2026-02-01"),
    ]

    # Publishing a new future rate preserves the stored historical result.
    catalog.write_text(json.dumps({"models": {"vendor/m": [
        {"effective_from": "2026-01-01", "input": 99, "output": 99,
         "cache_read": 99, "cache_write": 99, "source_url": "https://vendor/pricing"},
        {"effective_from": "2026-02-01", "input": 2, "output": 2,
         "cache_read": 2, "cache_write": 2, "source_url": "https://vendor/pricing"},
    ]}}), encoding="utf-8")
    pricing.recompute_daily_costs(conn, pricing.load_pricing(catalog), credits_per_usd=1.0)
    assert [r["cost_usd"] for r in conn.execute(
        "SELECT cost_usd FROM daily_usage ORDER BY date"
    )] == [1.0, 2.0]


def test_historical_rate_preserves_credit_card_and_cache_write_1h(conn, tmp_path):
    catalog = tmp_path / "dated-credits.json"
    catalog.write_text(json.dumps({"models": {"vendor/m": [{
        "effective_from": "2026-01-01", "input": 1, "output": 1,
        "cache_read": 1, "cache_write": 1, "cache_write_1h": 2,
        "credits": {"input": 1, "cache_read": 2, "output": 4},
    }]}}), encoding="utf-8")
    _insert_usage(conn, "rt", "vendor/m", "2026-01-01", 1_000_000, 1_000_000, 0, 0)
    rates = pricing.load_pricing(catalog)
    pricing.upsert_model_pricing(conn, rates)
    pricing.recompute_daily_costs(conn, rates, credits_per_usd=2.0)

    stored = conn.execute(
        "SELECT cache_write_1h_rate, credit_input_rate, credit_cache_read_rate, "
        "credit_output_rate FROM model_price_history"
    ).fetchone()
    assert tuple(stored) == (2.0, 1.0, 2.0, 4.0)
    assert conn.execute("SELECT cost_credits FROM daily_usage").fetchone()[0] == 5.0


def test_reconciliation_deduplicates_sanitized_variance_diagnostic(conn):
    """The same high variance alerts once and stores no invoice payload."""
    first = pricing.reconcile_billing_actual(
        conn, "anthropic", "2026-02", calculated_usd=90.0, actual_usd=100.0,
        threshold=0.05,
    )
    assert first["submitted_by"] is None
    del first["submitted_at"]
    del first["submitted_by"]
    assert first == {"provider": "anthropic", "period": "2026-02",
                     "variance_ratio": 0.1, "over_threshold": True,
                     "diagnostic_emitted": True, "currency": "USD"}
    assert pricing.reconcile_billing_actual(
        conn, "anthropic", "2026-02", calculated_usd=90.0, actual_usd=100.0,
        threshold=0.05,
    )["diagnostic_emitted"] is False
    stored = conn.execute("SELECT * FROM billing_reconciliation").fetchone()
    assert set(stored.keys()) == {"provider", "period", "calculated_usd",
                                  "actual_usd", "variance_ratio", "over_threshold",
                                  "diagnostic_emitted_at", "currency",
                                  "submitted_by", "submitted_at"}


def test_calculated_cost_for_period_sums_provider_month(conn):
    conn.execute(
        "INSERT INTO daily_usage (runtime_id, model, date, provider, "
        "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
        "synced_at, cost_usd) VALUES (?, ?, ?, ?, 0, 0, 0, 0, ?, ?)",
        ("rt1", "claude-opus-4-8", "2026-02-14", "anthropic", utcnow_iso(), 40.0),
    )
    conn.execute(
        "INSERT INTO daily_usage (runtime_id, model, date, provider, "
        "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
        "synced_at, cost_usd) VALUES (?, ?, ?, ?, 0, 0, 0, 0, ?, ?)",
        ("rt2", "claude-opus-4-8", "2026-02-20", "anthropic", utcnow_iso(), 50.0),
    )
    # Different month and different provider must not be counted.
    conn.execute(
        "INSERT INTO daily_usage (runtime_id, model, date, provider, "
        "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
        "synced_at, cost_usd) VALUES (?, ?, ?, ?, 0, 0, 0, 0, ?, ?)",
        ("rt3", "claude-opus-4-8", "2026-03-01", "anthropic", utcnow_iso(), 999.0),
    )
    conn.execute(
        "INSERT INTO daily_usage (runtime_id, model, date, provider, "
        "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
        "synced_at, cost_usd) VALUES (?, ?, ?, ?, 0, 0, 0, 0, ?, ?)",
        ("rt4", "gpt-5.6", "2026-02-10", "openai", utcnow_iso(), 999.0),
    )
    assert pricing.calculated_cost_for_period(conn, "anthropic", "2026-02") == 90.0
    assert pricing.calculated_cost_for_period(conn, "anthropic", "2026-04") == 0.0

    with pytest.raises(pricing.PricingError):
        pricing.calculated_cost_for_period(conn, "ANTHROPIC", "2026-02")


@pytest.mark.parametrize("period", [
    "2026-01", "2026-12", "0001-01", "9999-12",
])
def test_validate_billing_reconciliation_accepts_canonical_period(period):
    pricing.validate_billing_reconciliation("anthropic", period)


@pytest.mark.parametrize("period", [
    "2026-13", "2026-00", "0000-01", "26-01", "2026-1", "2026/01",
    "2026-01 ", " 2026-01", "2026-01\n", "",
    "２０２６-01",  # full-width Unicode digits
    "٢٠٢٦-01",  # Arabic-Indic digits
])
def test_validate_billing_reconciliation_rejects_non_canonical_period(period):
    with pytest.raises(pricing.PricingError):
        pricing.validate_billing_reconciliation("anthropic", period)


@pytest.mark.parametrize("amount", [
    "0", "0.5", "12.50", "1000000", "3.14159",
])
def test_parse_billing_amount_accepts_canonical_syntax(amount):
    assert pricing.parse_billing_amount(amount) == float(amount)


@pytest.mark.parametrize("amount", [
    None, "", "1_000", "1_000.5", "1,000", "-5", "+5", "5.", ".5",
    "1e5", "1E5", "inf", "Infinity", "nan", "NaN",
    " 5", "5 ", "5\n", "１０",  # full-width Unicode digits
    "٥",  # Arabic-Indic digit
])
def test_parse_billing_amount_rejects_non_canonical_syntax(amount):
    with pytest.raises(pricing.PricingError):
        pricing.parse_billing_amount(amount)


def test_reconcile_billing_actual_rejects_non_usd_currency(conn):
    with pytest.raises(pricing.PricingError):
        pricing.reconcile_billing_actual(
            conn, "anthropic", "2026-02", calculated_usd=90.0, actual_usd=100.0,
            currency="EUR",
        )


def test_reconcile_billing_actual_stores_provenance(conn):
    result = pricing.reconcile_billing_actual(
        conn, "anthropic", "2026-02", calculated_usd=90.0, actual_usd=95.0,
        submitted_by=7, submitted_at="2026-02-15T00:00:00Z",
    )
    assert result["currency"] == "USD"
    assert result["submitted_by"] == 7
    assert result["submitted_at"] == "2026-02-15T00:00:00Z"
    stored = conn.execute(
        "SELECT currency, submitted_by, submitted_at FROM billing_reconciliation "
        "WHERE provider = 'anthropic' AND period = '2026-02'"
    ).fetchone()
    assert tuple(stored) == ("USD", 7, "2026-02-15T00:00:00Z")


def test_billing_snapshot_degrades_without_optional_usage_columns():
    usage = sqlite3.connect(":memory:")
    usage.row_factory = sqlite3.Row
    usage.execute("CREATE TABLE daily_usage (date TEXT)")
    durable = sqlite3.connect(":memory:")
    durable.row_factory = sqlite3.Row
    durable.execute(
        "CREATE TABLE billing_reconciliation ("
        "user_id INTEGER, provider TEXT, period TEXT, calculated_usd REAL, "
        "actual_usd REAL, variance_ratio REAL, over_threshold INTEGER, "
        "diagnostic_emitted_at TEXT, currency TEXT, submitted_by INTEGER, "
        "submitted_at TEXT)"
    )
    try:
        assert pricing.billing_reconciliation_snapshot(
            usage, durable_conn=durable, user_id=1
        ) == {
            "rows": [], "coverage": {"periods_submitted": 0, "periods_total": 0}
        }
    finally:
        usage.close()
        durable.close()


def test_replaying_dated_catalog_is_idempotent(conn, tmp_path):
    catalog = tmp_path / "dated.json"
    catalog.write_text(json.dumps({"models": {"vendor/m": [
        {"effective_from": "2026-01-01", "input": 1, "output": 1,
         "cache_read": 1, "cache_write": 1},
    ]}}), encoding="utf-8")
    rates = pricing.load_pricing(catalog)
    pricing.upsert_model_pricing(conn, rates)
    pricing.upsert_model_pricing(conn, rates)
    assert conn.execute("SELECT COUNT(*) FROM model_price_history").fetchone()[0] == 1


def test_upsert_model_pricing_idempotent(conn):
    rates = pricing.load_pricing(PRICING_JSON)
    pricing.upsert_model_pricing(conn, rates)
    pricing.upsert_model_pricing(conn, rates)
    count = conn.execute("SELECT COUNT(*) FROM model_pricing").fetchone()[0]
    assert count == len(rates)


def test_unpriced_models_in_usage(conn):
    _insert_usage(conn, "rt1", "claude-opus-4-8", "2026-07-14", 1, 1, 1, 1)
    _insert_usage(conn, "rt2", "mystery-model", "2026-07-14", 1, 1, 1, 1)
    rates = pricing.load_pricing(PRICING_JSON)
    assert pricing.unpriced_models_in_usage(conn, rates) == ["mystery-model"]
