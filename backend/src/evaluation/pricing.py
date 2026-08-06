"""Optional, explicit model pricing for token-cost estimation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class ModelPrice:
    """Price per one million input and output tokens."""

    input_per_million_tokens: Decimal
    output_per_million_tokens: Decimal


class PricingCatalog:
    """Validated pricing catalog that never invents provider prices."""

    def __init__(self, *, currency: str, models: dict[str, ModelPrice]) -> None:
        """Initialize an immutable-by-convention in-memory price lookup."""
        self.currency = currency
        self.models = models

    @classmethod
    def from_file(cls, path: Path) -> PricingCatalog:
        """Load a pricing catalog from a local JSON file."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("pricing file root must be an object")
        currency = str(payload.get("currency", "")).strip().upper()
        raw_models = payload.get("models")
        if not currency or not isinstance(raw_models, dict):
            raise ValueError("pricing file requires currency and models")

        models: dict[str, ModelPrice] = {}
        for model, raw_price in raw_models.items():
            if not isinstance(raw_price, dict):
                raise ValueError(f"pricing entry for {model!r} must be an object")
            input_price = Decimal(str(raw_price["input_per_million_tokens"]))
            output_price = Decimal(str(raw_price["output_per_million_tokens"]))
            if input_price < 0 or output_price < 0:
                raise ValueError(f"pricing entry for {model!r} cannot be negative")
            models[str(model)] = ModelPrice(input_price, output_price)
        return cls(currency=currency, models=models)

    def estimate(
        self,
        *,
        model: str | None,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> tuple[float | None, str | None]:
        """Calculate cost when the exact configured model has a price."""
        price = self.models.get(model or "")
        if price is None:
            return None, None
        million = Decimal(1_000_000)
        cost = (
            Decimal(prompt_tokens) * price.input_per_million_tokens
            + Decimal(completion_tokens) * price.output_per_million_tokens
        ) / million
        return float(cost.quantize(Decimal("0.00000001"))), self.currency


def load_pricing_catalog(path: Path | None) -> tuple[PricingCatalog | None, str | None]:
    """Load optional pricing while returning a bounded startup warning on failure."""
    if path is None:
        return None, None
    try:
        return PricingCatalog.from_file(path), None
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return None, f"pricing_catalog_unavailable:{type(exc).__name__}"
