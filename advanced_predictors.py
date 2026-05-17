from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from collections import deque
import statistics

from predictors import ShortHorizonPredictor, Prediction, MarketFeatures

@dataclass(frozen=True)
class AdvancedMarketFeatures(MarketFeatures):
    """扩展的市场特征，增加了交易流和波动率。"""
    trade_flow: Decimal = Decimal("0")
    volatility: Decimal = Decimal("0")

class AdvancedPredictor(ShortHorizonPredictor):
    """一个使用订单簿不平衡度、微观价格、交易流和波动率的预测器。"""

    def __init__(
        self,
        impact_bps_per_imbalance: Decimal,
        min_confidence: Decimal,
        volatility_window: int = 10, # 计算波动率使用的窗口大小
    ) -> None:
        self.impact_bps_per_imbalance = impact_bps_per_imbalance
        self.min_confidence = min_confidence
        # 使用 deque 可以高效地在固定大小的窗口上进行操作
        self.mid_price_history = deque(maxlen=volatility_window)

    def predict(self, features: AdvancedMarketFeatures) -> Prediction:
        if features.quote.mid <= 0:
            return Prediction(Decimal("0"), Decimal("0"), Decimal("0"), "no_mid")

        # 1. 计算波动率因子
        self.mid_price_history.append(features.quote.mid)
        volatility = Decimal(statistics.stdev(self.mid_price_history)) if len(self.mid_price_history) > 1 else Decimal("0")

        # 2. 计算基础价格偏移 (来自原始ImbalancePredictor)
        base_edge_bps = self._microprice_edge_bps(features) + features.imbalance * self.impact_bps_per_imbalance

        # 3. 计算交易流调整量 (我们的新alpha!)
        # 规则：如果交易流为正（买方更激进），我们稍微调高预测价，反之亦然。
        # 调整的幅度可以根据置信度等进行缩放。
        trade_flow_adjustment_bps = features.trade_flow * Decimal("10") # 示例：每0.1的flow，价格偏移1 bps

        # 4. 组合所有因子
        total_edge_bps = base_edge_bps + trade_flow_adjustment_bps

        # 5. 风险调整：如果波动率过高，降低预测的偏移量，甚至不进行预测
        if volatility > (features.quote.mid * Decimal("0.01")): # 波动率超过1%
            total_edge_bps *= Decimal("0.5") # 偏移量减半
            reason_suffix = ";vol_dampen"
        else:
            reason_suffix = ""

        confidence = min(Decimal("1"), abs(features.imbalance) + abs(features.trade_flow))
        reason = (
            f"obi={features.imbalance:.2f};"
            f"flow={features.trade_flow:.2f};"
            f"vol={volatility:.4f};"
            f"edge={total_edge_bps:.2f}"
            f"{reason_suffix}"
        )

        if confidence < self.min_confidence:
            return Prediction(features.quote.mid, Decimal("0"), confidence, f"weak_{reason}")
            
        predicted_mid = features.quote.mid * (Decimal("1") + total_edge_bps / Decimal("10000"))
        return Prediction(predicted_mid=predicted_mid, edge_bps=total_edge_bps, confidence=confidence, reason=reason)

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