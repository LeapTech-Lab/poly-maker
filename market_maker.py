"""中性 Polymarket Market Making 主逻辑。

策略 intentionally 简单：在 YES/NO 两个 outcome token 上同时挂 maker 买单。
每个订单用美元金额换算 shares，并在每次刷新前取消旧单再重挂，方便控制小资金风险。
"""

from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP

from config import BotConfig
from market_sources import MarketSpec
from polymarket_adapter import FatalTradingError, PolymarketAdapter, Position, Quote, TokenConfig


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TargetOrder:
    token: TokenConfig
    side: str
    price: Decimal
    shares: Decimal
    reason: str

    @property
    def notional(self) -> Decimal:
        return self.price * self.shares


class MarketMaker:
    def __init__(self, config: BotConfig, adapter: PolymarketAdapter, spec: MarketSpec | None = None) -> None:
        self.config = config
        self.adapter = adapter
        self.spec = spec
        self.should_stop = False
        self.yes: TokenConfig
        self.no: TokenConfig
        self.condition_id: str
        self.initial_market_exposure = Decimal("0")

    def run(self) -> None:
        self.bootstrap()
        failures = 0
        while not self.should_stop:
            try:
                self.tick()
                failures = 0
                time.sleep(self.config.refresh_interval_seconds)
            except KeyboardInterrupt:
                self.should_stop = True
            except FatalTradingError:
                self.should_stop = True
                LOGGER.exception("Fatal trading error; stopping instead of retrying")
            except Exception:
                failures += 1
                sleep_for = min(30, 2**min(failures, 5))
                LOGGER.exception("Main loop error; reconnecting after %ss", sleep_for)
                time.sleep(sleep_for)

        self._cancel_working_orders()
        LOGGER.info("Stopped; old orders cancelled")

    def shutdown(self) -> None:
        self.should_stop = True
        self._cancel_working_orders()

    def bootstrap(self) -> None:
        self.config.validate()
        if self.spec is None:
            self.yes, self.no, self.condition_id = self.adapter.resolve_tokens()
        else:
            self.yes, self.no, self.condition_id = self.adapter.resolve_market_spec(self.spec)
        self._install_signal_handlers()

        if self.config.cancel_on_start:
            self._cancel_working_orders()

        self.initial_market_exposure = self._current_market_exposure()

        LOGGER.info(
            "Starting maker: condition_id=%s dry_run=%s order_notional=%s refresh=%ss initial_market_exposure=$%.2f",
            self.condition_id or "<token-only>",
            self.config.dry_run,
            self.config.order_notional_usdc,
            self.config.refresh_interval_seconds,
            float(self.initial_market_exposure),
        )

    def tick(self) -> None:
        tokens = (self.yes, self.no)
        quotes = {token.token_id: self.adapter.get_quote(token) for token in tokens}
        positions = self.adapter.get_positions(tokens, quotes=quotes)
        account_exposure = self.adapter.get_global_exposure()
        market_exposure = sum(position.notional for position in positions.values())
        bot_exposure = max(Decimal("0"), market_exposure - self.initial_market_exposure)
        risk_exposure = (
            account_exposure
            if self.config.count_existing_positions_in_global_limit
            else bot_exposure
        )
        unrealized_pnl = sum(position.unrealized_pnl for position in positions.values())
        rewards = self.adapter.today_rewards_estimate(self.condition_id)

        LOGGER.info(
            "Monitor market_exposure=$%.2f bot_exposure=$%.2f account_exposure=$%.2f uPnL=$%.2f rewards_today~$%.4f",
            float(market_exposure),
            float(bot_exposure),
            float(account_exposure),
            float(unrealized_pnl),
            float(rewards),
        )

        if risk_exposure >= self.config.max_global_exposure_usdc:
            LOGGER.warning(
                "Global exposure limit hit; risk_exposure=$%.2f max=$%.2f count_existing=%s",
                float(risk_exposure),
                float(self.config.max_global_exposure_usdc),
                self.config.count_existing_positions_in_global_limit,
            )
            self._cancel_working_orders()
            return

        if market_exposure >= self.config.max_market_exposure_usdc:
            LOGGER.warning("Market exposure limit hit; only risk-reducing quotes allowed")

        orders: list[TargetOrder] = []
        for token in tokens:
            quote = quotes[token.token_id]
            position = positions[token.token_id]
            maybe_order = self._build_buy_order(token, quote, position, market_exposure)
            if maybe_order:
                orders.append(maybe_order)
            maybe_sell = self._build_sell_order(token, quote, position)
            if maybe_sell:
                orders.append(maybe_sell)

        self._cancel_working_orders()
        for order in orders:
            self.adapter.place_limit_order(
                token=order.token,
                side=order.side,
                price=order.price,
                size=order.shares,
                dry_run=self.config.dry_run,
            )

    def _build_buy_order(
        self,
        token: TokenConfig,
        quote: Quote,
        position: Position,
        market_exposure: Decimal,
    ) -> TargetOrder | None:
        if quote.mid <= 0:
            LOGGER.warning("%s midpoint unavailable; skip quote", token.outcome)
            return None

        skew = Decimal("0")
        reason = "neutral"
        if position.notional >= self.config.max_token_exposure_usdc:
            LOGGER.info("%s token exposure limit hit; skip buy quote", token.outcome)
            return None
        if market_exposure >= self.config.max_market_exposure_usdc:
            LOGGER.info("Market exposure cap reached; skip new buy quote for %s", token.outcome)
            return None
        if position.notional >= self.config.inventory_skew_threshold_usdc:
            skew = self.config.inventory_skew_fraction
            reason = "inventory_skew"

        bid = quote.mid * (Decimal("1") - self.config.spread_fraction - skew)
        bid = max(self.config.min_price, min(self.config.max_price, bid))
        bid = self.adapter.round_price(bid, token.tick_size, ROUND_DOWN)
        if bid <= 0:
            return None

        remaining_market = self.config.max_market_exposure_usdc - market_exposure
        remaining_token = self.config.max_token_exposure_usdc - position.notional
        order_notional = min(self.config.order_notional_usdc, remaining_market, remaining_token)
        if order_notional <= 0:
            return None

        shares = self._shares_for_notional(order_notional, bid, token)
        if shares is None:
            LOGGER.info(
                "%s buy quote skipped: $%.2f notional is below min order size %s shares at price %s",
                token.outcome,
                float(order_notional),
                token.min_order_size,
                bid,
            )
            return None

        LOGGER.info(
            "%s quote mid=%s bid=%s shares=%s notional=$%.2f reason=%s",
            token.outcome,
            quote.mid,
            bid,
            shares,
            float(bid * shares),
            reason,
        )
        return TargetOrder(token=token, side="BUY", price=bid, shares=shares, reason=reason)

    def _build_sell_order(
        self,
        token: TokenConfig,
        quote: Quote,
        position: Position,
    ) -> TargetOrder | None:
        if position.size <= 0 or quote.mid <= 0:
            return None

        reason = "inventory_exit"
        skew = Decimal("0")
        if position.notional >= self.config.inventory_skew_threshold_usdc:
            skew = self.config.inventory_skew_fraction
            reason = "inventory_skew_exit"

        ask = quote.mid * (Decimal("1") + self.config.spread_fraction - skew)
        ask = max(self.config.min_price, min(self.config.max_price, ask))
        ask = self.adapter.round_price(ask, token.tick_size, ROUND_UP)
        if ask <= 0:
            return None

        target_notional = min(self.config.order_notional_usdc, position.notional)
        desired_shares = (target_notional / ask).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        shares = min(position.size, desired_shares)
        if shares < token.min_order_size:
            LOGGER.info(
                "%s sell quote skipped: shares=%s below min order size %s",
                token.outcome,
                shares,
                token.min_order_size,
            )
            return None

        LOGGER.info(
            "%s sell quote mid=%s ask=%s shares=%s notional=$%.2f reason=%s",
            token.outcome,
            quote.mid,
            ask,
            shares,
            float(ask * shares),
            reason,
        )
        return TargetOrder(token=token, side="SELL", price=ask, shares=shares, reason=reason)

    def _shares_for_notional(
        self,
        order_notional: Decimal,
        price: Decimal,
        token: TokenConfig,
    ) -> Decimal | None:
        desired_shares = (order_notional / price).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        if desired_shares >= token.min_order_size:
            return desired_shares

        min_notional = token.min_order_size * price
        remaining_market = self.config.max_market_exposure_usdc - self._current_market_exposure()
        if min_notional <= self.config.order_notional_usdc and min_notional <= remaining_market:
            return token.min_order_size
        return None

    def _install_signal_handlers(self) -> None:
        def stop(_signum, _frame) -> None:
            LOGGER.info("Stop signal received")
            self.should_stop = True

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)

    def _current_market_exposure(self) -> Decimal:
        positions = self.adapter.get_positions((self.yes, self.no))
        return sum(position.notional for position in positions.values())

    def _cancel_working_orders(self) -> None:
        if self.condition_id:
            self.adapter.cancel_market_orders(self.condition_id)
            return
        self.adapter.cancel_token_orders(self.yes.token_id)
        self.adapter.cancel_token_orders(self.no.token_id)
