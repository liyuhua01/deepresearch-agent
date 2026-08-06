"""Tests for explicit, model-specific cost estimation."""

from __future__ import annotations

import json

from evaluation.pricing import PricingCatalog, load_pricing_catalog


def test_exact_model_price_calculates_cost(tmp_path) -> None:
    pricing_file = tmp_path / "pricing.json"
    pricing_file.write_text(
        json.dumps(
            {
                "currency": "USD",
                "models": {
                    "model-a": {
                        "input_per_million_tokens": 0.5,
                        "output_per_million_tokens": 1.5,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    catalog = PricingCatalog.from_file(pricing_file)

    cost, currency = catalog.estimate(
        model="model-a",
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
    )

    assert cost == 2.0
    assert currency == "USD"


def test_unknown_model_and_invalid_catalog_do_not_invent_cost(tmp_path) -> None:
    catalog = PricingCatalog(currency="USD", models={})
    assert catalog.estimate(
        model="unknown", prompt_tokens=100, completion_tokens=100
    ) == (None, None)

    invalid_file = tmp_path / "invalid.json"
    invalid_file.write_text("not-json", encoding="utf-8")
    loaded, warning = load_pricing_catalog(invalid_file)
    assert loaded is None
    assert warning == "pricing_catalog_unavailable:JSONDecodeError"
