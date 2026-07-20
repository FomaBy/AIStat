"""Tests for the pricing / cost / credits module (stage 2)."""

import json
from pathlib import Path

import pytest

from aistat import pricing
from aistat.db import utcnow_iso

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
    assert (opus.input, opus.cache_read, opus.output) == (62.5, 6.25, 375.0)
    assert (opus.input, opus.cache_read, opus.output) == \
           (terra.input, terra.cache_read, terra.output)


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
                  "claude-haiku-4-5-20251001", "gpt-5.6-sol", "gpt-5.6-terra"):
        assert model in rates, model
        rate = rates[model]
        assert not rate.unpriced
        assert rate.source_url and rate.source_url.startswith("https://")
        assert rate.captured_at  # every rate records when it was taken

    opus = rates["claude-opus-4-8"]
    assert (opus.input, opus.output, opus.cache_read, opus.cache_write) == (5.0, 25.0, 0.5, 6.25)
    sol = rates["gpt-5.6-sol"]
    assert (sol.input, sol.output, sol.cache_read) == (5.0, 30.0, 0.5)


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
    # (1000*62.5 + 2000*375 + 1_000_000*6.25) / 1e6. cache_write excluded.
    assert opus["cost_credits"] == pytest.approx(7.0625)

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


def test_recompute_is_idempotent(conn):
    _insert_usage(conn, "rt1", "gpt-5.6-sol", "2026-07-14", 1_000_000, 0, 0, 0)
    rates = pricing.load_pricing(PRICING_JSON)
    pricing.recompute_daily_costs(conn, rates, credits_per_usd=1.0)
    first = conn.execute("SELECT cost_usd FROM daily_usage").fetchone()[0]
    pricing.recompute_daily_costs(conn, rates, credits_per_usd=1.0)
    second = conn.execute("SELECT cost_usd FROM daily_usage").fetchone()[0]
    assert first == second == pytest.approx(5.0)  # 1M input * $5/M


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
