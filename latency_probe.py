"""Measure local Polymarket order latency.

Default mode is safe and does not submit orders. It measures quote fetch and
local signing time, then estimates the prediction horizon. Passing
--post-probe submits one tiny FOK order, which either fills fully or cancels.
"""

from __future__ import annotations

import argparse
import statistics
import time
from decimal import Decimal, ROUND_DOWN

from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side

from config import BotConfig
from polymarket_adapter import OrderBookSnapshot, PolymarketAdapter, TokenConfig
# 确保导入了 MarketFeatures 和您要使用的 Predictor
from predictors import ImbalancePredictor, MarketFeatures


def _ms(start: int, end: int) -> float:
  return (end - start) / 1_000_000


def _p95(values: list[float]) -> float:
  if len(values) < 2:
    return values[0] if values else 0
  return statistics.quantiles(values, n=20, method="inclusive")[18]


def _safe_probe_price(adapter: PolymarketAdapter, token: TokenConfig, snapshot: OrderBookSnapshot) -> Decimal:
  below_ask = snapshot.quote.ask - token.tick_size
  price = min(snapshot.quote.bid, below_ask)
  price = max(Decimal("0.01"), price)
  return adapter.round_price(price, token.tick_size, ROUND_DOWN)


def main() -> None:
  parser = argparse.ArgumentParser(description="Measure quote/sign/post latency for the current market")
  parser.add_argument("--samples", type=int, default=20, help="number of samples")
  parser.add_argument("--token", choices=("yes", "no"), default="yes", help="token side to probe")
  parser.add_argument("--post-probe", action="store_true", help="submit a tiny FOK order")
  args = parser.parse_args()

  config = BotConfig()
  config.validate()
  adapter = PolymarketAdapter(config)
  yes, no, _condition_id = adapter.resolve_tokens()
  token = yes if args.token == "yes" else no
  can_sign = bool(config.private_key)
  if args.post_probe and (config.dry_run or not can_sign):
    raise RuntimeError("--post-probe requires DRY_RUN=false and a configured PK")

  quote_ms: list[float] = []
  # 这个列表现在会存储 "预测+签名" 的时间
  local_processing_ms: list[float] = [] 
  post_ms: list[float] = []
  cancel_ms: list[float] = []

  # 从 BotConfig 初始化 Predictor，以保证参数一致
  predictor = ImbalancePredictor(
      impact_bps_per_imbalance=config.prediction_edge_bps,
      min_confidence=config.min_prediction_confidence,
  )

  for _idx in range(args.samples):
    # 1. 获取行情
    t0 = time.perf_counter_ns()
    snapshot = adapter.get_order_book_snapshot(token)
    t1 = time.perf_counter_ns()
    
    # 2. 完整地执行所有本地计算
    signed_order = None
    if can_sign:
        # a. 模拟预测
        _ = predictor.predict(
            MarketFeatures(
                token=token,
                quote=snapshot.quote,
                imbalance=Decimal("0"),
                latency_ms=0,
                best_bid_size=snapshot.bids[0][1] if snapshot.bids else Decimal("0"),
                best_ask_size=snapshot.asks[0][1] if snapshot.asks else Decimal("0"),
            )
        )
        
        # b. 计算价格和大小
        price = _safe_probe_price(adapter, token, snapshot)
        raw_size = (config.order_notional_usdc / price)
        quantized_size = raw_size.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
        size = max(token.min_order_size, quantized_size)

        # c. 签名订单
        signed_order = adapter.client.create_order(
            OrderArgs(
                token_id=token.token_id,
                price=float(price),
                side=Side.BUY,
                size=float(size),
            ),
            PartialCreateOrderOptions(tick_size=str(token.tick_size), neg_risk=token.neg_risk),
        )
    t2 = time.perf_counter_ns() # 标记所有本地计算完成

    quote_ms.append(_ms(t0, t1))
    if can_sign:
        # 测量从获取行情后，到完成所有本地计算（预测+签名等）的总时间
        local_processing_ms.append(_ms(t1, t2))

    if args.post_probe:
      t3 = time.perf_counter_ns()
      response = adapter.client.post_order(signed_order, order_type=OrderType.FOK, post_only=False)
      t4 = time.perf_counter_ns()
      order_id = ""
      if isinstance(response, dict):
        order_id = str(response.get("orderID") or response.get("order_id") or response.get("id") or "")
      if order_id:
        adapter.cancel_orders([order_id])
      elif config.order_type != "FOK":
        adapter.cancel_token_orders(token.token_id)
      t5 = time.perf_counter_ns()
      post_ms.append(_ms(t3, t4))
      cancel_ms.append(_ms(t4, t5))

    time.sleep(0.1)

  print(f"token={token.outcome} token_id={token.token_id}")
  print(f"quote_fetch_ms avg={statistics.mean(quote_ms):.2f} p95={_p95(quote_ms):.2f}")
  if local_processing_ms:
    total_safe = [q + s for q, s in zip(quote_ms, local_processing_ms)]
    print(f"local_processing_ms avg={statistics.mean(local_processing_ms):.2f} p95={_p95(local_processing_ms):.2f}")
    print(f"safe_horizon_ms avg={statistics.mean(total_safe):.2f} p95={_p95(total_safe):.2f}")
  else:
    print("local_sign_ms skipped because PK is not configured")
  if post_ms:
    total_live = [q + s + p for q, s, p in zip(quote_ms, local_processing_ms, post_ms)]
    print(f"post_ack_ms  avg={statistics.mean(post_ms):.2f} p95={_p95(post_ms):.2f}")
    print(f"cancel_ack_ms avg={statistics.mean(cancel_ms):.2f} p95={_p95(cancel_ms):.2f}")
    print(f"live_horizon_ms avg={statistics.mean(total_live):.2f} p95={_p95(total_live):.2f}")
  print("Use the p95 horizon as PREDICTION_LATENCY_MS for conservative quoting.")


if __name__ == "__main__":
  main()
