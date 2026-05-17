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
    best_bid_size: Decimal = Decimal("0")
    best_ask_size: Decimal = Decimal("0")


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
    """Short-horizon fair-price estimate from microprice plus book imbalance."""

    def __init__(self, impact_bps_per_imbalance: Decimal, min_confidence: Decimal) -> None:
        self.impact_bps_per_imbalance = impact_bps_per_imbalance
        self.min_confidence = min_confidence

    def predict(self, features: MarketFeatures) -> Prediction:
        if features.quote.mid <= 0:
            return Prediction(Decimal("0"), Decimal("0"), Decimal("0"), "no_mid")

        edge_bps = self._microprice_edge_bps(features) + features.imbalance * self.impact_bps_per_imbalance
        confidence = min(Decimal("1"), abs(features.imbalance))
        reason = (
            f"obi={features.imbalance:.4f};"
            f"micro_edge_bps={self._microprice_edge_bps(features):.4f};"
            f"horizon={features.latency_ms}ms"
        )
        if confidence < self.min_confidence:
            return Prediction(features.quote.mid, Decimal("0"), confidence, f"weak_{reason}")
        predicted_mid = features.quote.mid * (Decimal("1") + edge_bps / Decimal("10000"))
        return Prediction(predicted_mid=predicted_mid, edge_bps=edge_bps, confidence=confidence, reason=reason)

    @staticmethod
    def _microprice_edge_bps(features: MarketFeatures) -> Decimal:
        total_size = features.best_bid_size + features.best_ask_size
        if total_size <= 0 or features.quote.mid <= 0:
            return Decimal("0")
        microprice = (
            features.quote.ask * features.best_bid_size
            + features.quote.bid * features.best_ask_size
        ) / total_size
        return (microprice - features.quote.mid) / features.quote.mid * Decimal("10000")
