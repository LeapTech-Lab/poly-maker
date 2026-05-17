"""Short-horizon price prediction hooks for market making.

The default predictor is deliberately conservative.  It uses only order book
imbalance as a tiny mid-price nudge, but exposes the same interface that a
factor stack or model can implement later.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from polymarket_adapter import Quote, TokenConfig


@dataclass(frozen=True)
class MarketFeatures:
    token: TokenConfig
    quote: Quote
    imbalance: Decimal
    latency_ms: int


@dataclass(frozen=True)
class Prediction:
    predicted_mid: Decimal
    edge_bps: Decimal
    confidence: Decimal
    reason: str


class ShortHorizonPredictor:
    def predict(self, features: MarketFeatures) -> Prediction:
        raise NotImplementedError


class ImbalancePredictor(ShortHorizonPredictor):
    """Small, bounded mid-price adjustment from top-of-book imbalance."""

    def __init__(self, impact_bps_per_imbalance: Decimal, min_confidence: Decimal) -> None:
        self.impact_bps_per_imbalance = impact_bps_per_imbalance
        self.min_confidence = min_confidence

    def predict(self, features: MarketFeatures) -> Prediction:
        if features.quote.mid <= 0:
            return Prediction(Decimal("0"), Decimal("0"), Decimal("0"), "no_mid")

        edge_bps = features.imbalance * self.impact_bps_per_imbalance
        confidence = min(Decimal("1"), abs(features.imbalance))
        reason = f"obi={features.imbalance:.4f};horizon={features.latency_ms}ms"
        if confidence < self.min_confidence:
            return Prediction(features.quote.mid, Decimal("0"), confidence, f"weak_{reason}")
        predicted_mid = features.quote.mid * (Decimal("1") + edge_bps / Decimal("10000"))
        return Prediction(predicted_mid=predicted_mid, edge_bps=edge_bps, confidence=confidence, reason=reason)
