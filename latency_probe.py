"""Measure local Polymarket order latency.

Default mode is safe and does not submit orders.  It measures quote fetch and
local signing time, then estimates the prediction horizon.  Passing
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
    sign_ms: list[float] = []
    post_ms: list[float] = []
    cancel_ms: list[float] = []

    for _idx in range(args.samples):
        t0 = time.perf_counter_ns()
        snapshot = adapter.get_order_book_snapshot(token)
        t1 = time.perf_counter_ns()

        price = _safe_probe_price(adapter, token, snapshot)
        size = max(token.min_order_size, (config.order_notional_usdc / price).quantize(Decimal("0.000001")))
        t2 = time.perf_counter_ns()
        signed_order = None
        if can_sign:
            signed_order = adapter.client.create_order(
                OrderArgs(
                    token_id=token.token_id,
                    price=float(price),
                    side=Side.BUY,
                    size=float(size),
                ),
                PartialCreateOrderOptions(tick_size=str(token.tick_size), neg_risk=token.neg_risk),
            )
        t3 = time.perf_counter_ns()

        quote_ms.append(_ms(t0, t1))
        if can_sign:
            sign_ms.append(_ms(t2, t3))

        if args.post_probe:
            t4 = time.perf_counter_ns()
            response = adapter.client.post_order(signed_order, order_type=OrderType.FOK, post_only=False)
            t5 = time.perf_counter_ns()
            order_id = ""
            if isinstance(response, dict):
                order_id = str(response.get("orderID") or response.get("order_id") or response.get("id") or "")
            if order_id:
                adapter.cancel_orders([order_id])
            elif config.order_type != "FOK":
                adapter.cancel_token_orders(token.token_id)
            t6 = time.perf_counter_ns()
            post_ms.append(_ms(t4, t5))
            cancel_ms.append(_ms(t5, t6))

        time.sleep(0.1)

    print(f"token={token.outcome} token_id={token.token_id}")
    print(f"quote_fetch_ms avg={statistics.mean(quote_ms):.2f} p95={_p95(quote_ms):.2f}")
    if sign_ms:
        total_safe = [q + s for q, s in zip(quote_ms, sign_ms)]
        print(f"local_sign_ms  avg={statistics.mean(sign_ms):.2f} p95={_p95(sign_ms):.2f}")
        print(f"safe_horizon_ms avg={statistics.mean(total_safe):.2f} p95={_p95(total_safe):.2f}")
    else:
        print("local_sign_ms skipped because PK is not configured")
    if post_ms:
        total_live = [q + s + p for q, s, p in zip(quote_ms, sign_ms, post_ms)]
        print(f"post_ack_ms    avg={statistics.mean(post_ms):.2f} p95={_p95(post_ms):.2f}")
        print(f"cancel_ack_ms  avg={statistics.mean(cancel_ms):.2f} p95={_p95(cancel_ms):.2f}")
        print(f"live_horizon_ms avg={statistics.mean(total_live):.2f} p95={_p95(total_live):.2f}")
    print("Use the p95 horizon as PREDICTION_LATENCY_MS for conservative quoting.")


if __name__ == "__main__":
    main()
