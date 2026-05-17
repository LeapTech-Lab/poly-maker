"""短期预测驱动的 Polymarket 做市逻辑。

策略使用预测价格进行报价，支持 GTC + Post-only 模式以确保作为 Maker 提供流动性。
当配置为 GTC 时，每轮 tick 会更新挂单，使用 Post-only 防止意外跨盘口成交。
"""

from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP

from config import BotConfig
from market_sources import MarketSpec
from polymarket_adapter import FatalTradingError, PolymarketAdapter, Position, Quote, TokenConfig
from py_clob_client_v2.exceptions import PolyApiException
from predictors import ImbalancePredictor, MarketFeatures, ShortHorizonPredictor
# MODIFICATION: 导入新的预测器和数据记录器
from advanced_predictors import AdvancedPredictor, AdvancedMarketFeatures
from data_recorder import DataRecorder

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
    def __init__(
        self,
        config: BotConfig,
        adapter: PolymarketAdapter,
        spec: MarketSpec | None = None,
        predictor: ShortHorizonPredictor | None = None,
    ) -> None:
        self.config = config
        self.adapter = adapter
        self.spec = spec
        self.predictor = predictor or ImbalancePredictor(
            impact_bps_per_imbalance=config.prediction_edge_bps,
            min_confidence=config.min_prediction_confidence,
        )
        self.data_recorder: DataRecorder | None = None
        self.should_stop = False
        self.yes: TokenConfig
        self.no: TokenConfig
        self.condition_id: str
        self.initial_market_exposure = Decimal("0")
        self.last_midpoints: dict[str, Decimal] = {}
        self.risk_off = False

        self.last_placed_orders: list[TargetOrder] = []

        # --- 新增：动态订单金额状态 ---
        # 存储当前有效的下单名义价值。初始化为配置值。
        self.dynamic_order_notional: Decimal = config.order_notional_usdc
        # 记录上次成功下单的时间，用于逐步恢复金额
        self.last_success_order_time: float = time.time()
        # --- 结束 ---

    def run(self) -> None:
        self.bootstrap()
        failures = 0
        while not self.should_stop:
            try:
                self.tick()
                failures = 0
                time.sleep(self.next_sleep_seconds())
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
        # MODIFICATION: 停止数据记录器
        if self.data_recorder:
            self.data_recorder.stop()


    def shutdown(self) -> None:
        self.should_stop = True
        self._cancel_working_orders()

    def next_sleep_seconds(self) -> float:
        return float(self.config.refresh_interval_seconds)

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


        # MODIFICATION: 在 bootstrap 中初始化预测器和数据记录器
        if self.predictor is None:
            self.predictor = AdvancedPredictor(
                impact_bps_per_imbalance=self.config.prediction_edge_bps,
                min_confidence=self.config.min_prediction_confidence,
            )
        if self.data_recorder is None and self.condition_id:
            self.data_recorder = DataRecorder(self.condition_id)

        if self.config.cancel_on_start:
            self._cancel_working_orders()

        self.initial_market_exposure = self._current_market_exposure()

        LOGGER.info(
            "Starting Market Maker: condition_id=%s dry_run=%s order_type=%s post_only=%s order_notional=%s refresh=%ss",
            self.condition_id or "<token-only>",
            self.config.dry_run,
            self.config.order_type,
            self.config.post_only,
            self.config.order_notional_usdc,
            self.config.refresh_interval_seconds,
        )

    # MODIFICATION: 在 tick 的开头添加 _calculate_trade_flow 辅助函数
    def _calculate_trade_flow(self, trades: list) -> Decimal:
        """计算订单方向性因子（交易流）。
      
        返回一个在 -1 到 1 之间的值。
        正值表示买方更激进（吃掉卖单），负值表示卖方更激进。
        """
        if not trades:
            return Decimal("0")
      
        taker_buy_volume = sum(Decimal(t['size']) for t in trades if t['side'] == 'buy')
        taker_sell_volume = sum(Decimal(t['size']) for t in trades if t['side'] == 'sell')
        total_volume = taker_buy_volume + taker_sell_volume

        if total_volume == 0:
            return Decimal("0")
          
        return (taker_buy_volume - taker_sell_volume) / total_volume
    
    def tick(self) -> None:
        # --- 新增：动态金额恢复逻辑 ---
        # 如果动态金额低于配置值，并且距离上次成功下单已超过一段时间（例如30秒），
        # 就尝试将金额逐步恢复。
        if (self.dynamic_order_notional < self.config.order_notional_usdc and
            time.time() - self.last_success_order_time > 30):

            # 每次恢复一点点，比如恢复10%的差距
            new_notional = self.dynamic_order_notional + (self.config.order_notional_usdc - self.dynamic_order_notional) * Decimal("0.1")
            self.dynamic_order_notional = min(new_notional, self.config.order_notional_usdc)
            LOGGER.info("[STRATEGY] Gradually restoring order notional to $%.2f", self.dynamic_order_notional)

        # --- 结束 ---

        # 对于 GTC 模式，每轮需要先清理之前的挂单
        if self.config.order_type == "GTC":
            self._cancel_working_orders()

        # 1. 获取所有市场数据
        tokens = (self.yes, self.no)
        snapshots = {token.token_id: self.adapter.get_order_book_snapshot(token) for token in tokens}
        quotes = {token_id: snapshot.quote for token_id, snapshot in snapshots.items()}
        positions = self.adapter.get_positions(tokens, quotes=quotes)

        # MODIFICATION: 获取最近成交和计算交易流
        recent_trades_yes = self.adapter.get_recent_trades(self.yes.token_id)
        trade_flow_yes = self._calculate_trade_flow(recent_trades_yes)
        # 对于二元期权市场，通常只分析一个方向的交易流就足够了
        trade_flows = {self.yes.token_id: trade_flow_yes, self.no.token_id: -trade_flow_yes}


        orderbook_imbalances = {
            token.token_id: self._calculate_orderbook_imbalance(
                snapshots[token.token_id].bids,
                snapshots[token.token_id].asks,
            )
            for token in tokens
        }

        # 2. 计算风险敞口和PnL
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

        # [STAT] 每轮状态概览
        LOGGER.info(
            "[STATUS] Market=%s | AccountExposure=$%.2f | MarketExposure=$%.2f | uPnL=$%.2f | Rewards~$%.4f",
            self.condition_id or "N/A",
            float(account_exposure),
            float(market_exposure),
            float(unrealized_pnl),
            float(rewards),
        )

        # 检查持仓变化，辅助判断是否成交
        for token in tokens:
            pos = positions[token.token_id]
            if pos.size > 0:
                LOGGER.info(
                    "[POSITION] %s: Size=%s | AvgPrice=%s | CurPrice=%s | uPnL=$%.2f",
                    token.outcome,
                    pos.size,
                    pos.avg_price,
                    pos.current_price,
                    float(pos.unrealized_pnl),
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

        close_only = self.risk_off or self._is_close_only_window()
        if close_only:
            LOGGER.warning("Close-only mode active; new buy quotes are disabled")

        if self._market_loss_limit_hit(unrealized_pnl):
            close_only = True
            self.risk_off = self.config.risk_off_after_stop
            LOGGER.warning("[RISK] Market loss limit hit (uPnL=$%.2f); enabling close-only mode", float(unrealized_pnl))

        # --- 新增：计算市场信念因子 ---
        market_conviction_yes = quotes[self.yes.token_id].mid

        orders: list[TargetOrder] = []
        tick_data_to_log = {} # MODIFICATION: 准备记录数据

        for token in tokens:
            quote = quotes[token.token_id]
            position = positions[token.token_id]
            imbalance = orderbook_imbalances[token.token_id]
            best_bid_size, best_ask_size = self._top_of_book_sizes(
                snapshots[token.token_id].bids,
                snapshots[token.token_id].asks,
            )

            # MODIFICATION: 构建 AdvancedMarketFeatures 并进行预测
            advanced_features = AdvancedMarketFeatures(
                token=token,
                quote=quote,
                imbalance=imbalance,
                latency_ms=self.config.prediction_latency_ms,
                best_bid_size=best_bid_size,
                best_ask_size=best_ask_size,
                trade_flow=trade_flows.get(token.token_id, Decimal("0")),
                # 波动率是在 predictor 内部计算和维护的
            )

            prediction = self.predictor.predict(
                MarketFeatures(
                    token=token,
                    quote=quote,
                    imbalance=imbalance,
                    latency_ms=self.config.prediction_latency_ms,
                    best_bid_size=best_bid_size,
                    best_ask_size=best_ask_size,
                )
            )
            
            # 记录盘口详情
            LOGGER.info(
                "[MARKET] %s: Mid=%s | Bid=%s x %s | Ask=%s x %s | LastMid=%s",
                token.outcome,
                quote.mid,
                quote.bid,
                best_bid_size,
                quote.ask,
                best_ask_size,
                self.last_midpoints.get(token.token_id, "N/A"),
            )
            LOGGER.info(
                "[PREDICT] %s: PredMid=%s | EdgeBps=%s | Confidence=%s | Reason=%s",
                token.outcome,
                prediction.predicted_mid,
                prediction.edge_bps,
                prediction.confidence,
                prediction.reason,
            )


            # MODIFICATION: 记录本轮的所有数据
            if self.data_recorder and token.outcome == "YES": # 只记录一次避免重复
                tick_data_to_log = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "mid_price": quote.mid,
                    "bid_price": quote.bid,
                    "ask_price": quote.ask,
                    "best_bid_size": best_bid_size,
                    "best_ask_size": best_ask_size,
                    "imbalance": imbalance,
                    "trade_flow": trade_flows.get(token.token_id, Decimal("0")),
                    "predicted_mid": prediction.predicted_mid,
                    "prediction_reason": prediction.reason,
                    "unrealized_pnl": unrealized_pnl,
                }

            allow_new_buy = not close_only and not self._position_risk_off(token, quote, position)
            if self._midpoint_jump_detected(token, quote):
                allow_new_buy = False
                LOGGER.warning("[STRATEGY] %s midpoint jumped too fast; skipping buy", token.outcome)

            maybe_order = (
                self._build_buy_order(
                    token,
                    quote,
                    position,
                    market_exposure,
                    prediction.predicted_mid,
                    market_conviction_yes=market_conviction_yes,
                )
                if allow_new_buy
                else None
            )
            if maybe_order:
                orders.append(maybe_order)
            
            maybe_sell = self._build_sell_order(
                token,
                quote,
                position,
                prediction.predicted_mid,
                force_exit=close_only,
                market_conviction_yes=market_conviction_yes,
            )
            if maybe_sell:
                orders.append(maybe_sell)

            self.last_midpoints[token.token_id] = quote.mid
        
        # MODIFICATION: 在下单前，将本轮数据发送到记录器队列
        if self.data_recorder and tick_data_to_log:
            self.data_recorder.record(tick_data_to_log)

        LOGGER.info("[STRATEGY] Placing %d %s targets", len(orders), self.config.order_type)
        
        for order in orders:
            LOGGER.info(
                "[TRADE] Placing %s %s: Price=%s | Shares=%s | Notional=$%.2f | Reason=%s",
                order.side,
                order.token.outcome,
                order.price,
                order.shares,
                float(order.notional),
                order.reason,
            )
            # self.adapter.place_limit_order(
            #     token=order.token,
            #     side=order.side,
            #     price=order.price,
            #     size=order.shares,
            #     dry_run=self.config.dry_run,
            # )
            self._execute_order_with_retry(order)

        self.last_placed_orders = orders

    def _build_buy_order(
        self,
        token: TokenConfig,
        quote: Quote,
        position: Position,
        market_exposure: Decimal,
        predicted_mid: Decimal,
        # --- 新增接收这个参数 ---
        market_conviction_yes: Decimal,
    ) -> TargetOrder | None:
        if quote.mid <= 0:
            LOGGER.warning("%s midpoint unavailable; skip quote", token.outcome)
            return None
        if predicted_mid <= 0:
            predicted_mid = quote.mid

        reason = "predicted_buy"
        if position.notional >= self.config.max_token_exposure_usdc:
            LOGGER.info("%s token exposure limit hit; skip buy quote", token.outcome)
            return None
        if market_exposure >= self.config.max_market_exposure_usdc:
            LOGGER.info("Market exposure cap reached; skip new buy quote for %s", token.outcome)
            return None
        if position.notional >= self.config.inventory_skew_threshold_usdc:
            reason = "inventory_skew_buy"

        # 盈利阈值检查：如果预测价格甚至不如当前买一价，则不挂单
        if predicted_mid <= quote.bid:
            LOGGER.info(
                "%s buy skipped: predicted_mid=%s is not attractive enough (<= bid %s)",
                token.outcome,
                predicted_mid,
                quote.bid,
            )
            return None

        # bid = self._calculate_limit_price(token, "BUY", predicted_mid, quote, position)
        bid = self._calculate_limit_price(token, "BUY", predicted_mid, quote, position, market_conviction_yes)
    
        LOGGER.debug(
            "[DEBUG] %s Buy Calculation: PredMid=%s Ask=%s -> Limit=%s",
            token.outcome,
            predicted_mid,
            quote.ask,
            bid,
        )

        if bid <= 0:
            return None

        remaining_market = self.config.max_market_exposure_usdc - market_exposure
        remaining_token = self.config.max_token_exposure_usdc - position.notional
        order_notional = min(self.dynamic_order_notional, remaining_market, remaining_token)
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
            "%s buy quote mid=%s ask=%s limit=%s shares=%s notional=$%.2f reason=%s",
            token.outcome,
            quote.mid,
            quote.ask,
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
        predicted_mid: Decimal,
        # --- 新增接收这个参数 ---
        market_conviction_yes: Decimal,
        force_exit: bool = False,
    ) -> TargetOrder | None:
        if position.size <= 0 or quote.mid <= 0:
            return None
        if predicted_mid <= 0:
            predicted_mid = quote.mid

        reason = "inventory_exit"
        force_sell = force_exit
        if force_exit or self._position_risk_off(token, quote, position):
            force_sell = True
            reason = "risk_exit"
        elif position.notional >= self.config.inventory_skew_threshold_usdc:
            reason = "inventory_skew_exit"

        # 盈利阈值检查：如果预测价格甚至高于当前卖一价，则不挂卖单
        if not force_sell and predicted_mid >= quote.ask:
            LOGGER.info(
                "%s sell skipped: predicted_mid=%s is not attractive enough (>= ask %s)",
                token.outcome,
                predicted_mid,
                quote.ask,
            )
            return None

        # ask = self._calculate_limit_price(token, "SELL", predicted_mid, quote, position)
        # --- 修改这里的调用 ---
        ask = self._calculate_limit_price(token, "SELL", predicted_mid, quote, position, market_conviction_yes)
   
        if ask <= 0:
            return None

        target_notional = min(self.dynamic_order_notional, position.notional)
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
            "%s sell quote mid=%s bid=%s limit=%s shares=%s notional=$%.2f reason=%s",
            token.outcome,
            quote.mid,
            quote.bid,
            ask,
            shares,
            float(ask * shares),
            reason,
        )
        return TargetOrder(token=token, side="SELL", price=ask, shares=shares, reason=reason)

    def _position_return(self, position: Position) -> Decimal:
        if position.size <= 0 or position.avg_price <= 0:
            return Decimal("0")
        return (position.current_price - position.avg_price) / position.avg_price

    def _position_risk_off(self, token: TokenConfig, quote: Quote, position: Position) -> bool:
        ret = self._position_return(position)
        if position.size <= 0:
            return False
        if ret <= -self.config.stop_loss_fraction:
            LOGGER.warning(
                "%s stop loss hit: return=%.2f%% avg=%s mid=%s",
                token.outcome,
                float(ret * Decimal("100")),
                position.avg_price,
                quote.mid,
            )
            self.risk_off = self.config.risk_off_after_stop
            return True
        if ret >= self.config.take_profit_fraction:
            LOGGER.info(
                "%s take profit hit: return=%.2f%% avg=%s mid=%s",
                token.outcome,
                float(ret * Decimal("100")),
                position.avg_price,
                quote.mid,
            )
            return True
        return False

    def _market_loss_limit_hit(self, unrealized_pnl: Decimal) -> bool:
        return unrealized_pnl <= -self.config.max_market_loss_usdc

    def _midpoint_jump_detected(self, token: TokenConfig, quote: Quote) -> bool:
        last_mid = self.last_midpoints.get(token.token_id)
        if not last_mid or last_mid <= 0:
            return False
        move = abs(quote.mid - last_mid) / last_mid
        return move >= self.config.max_midpoint_move_fraction

    def _is_close_only_window(self) -> bool:
        end_date_iso = self.yes.end_date_iso or self.no.end_date_iso
        if not end_date_iso or self.config.close_only_hours_before_end <= 0:
            return False
        try:
            normalized = end_date_iso.replace("Z", "+00:00")
            end_dt = datetime.fromisoformat(normalized)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        seconds_left = (end_dt - datetime.now(timezone.utc)).total_seconds()
        return seconds_left <= self.config.close_only_hours_before_end * 3600

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
            self.last_placed_orders = []
            return
        self.adapter.cancel_token_orders(self.yes.token_id)
        self.adapter.cancel_token_orders(self.no.token_id)
        self.last_placed_orders = []

    def _calculate_orderbook_imbalance(
        self,
        bids: list[tuple[Decimal, Decimal]],
        asks: list[tuple[Decimal, Decimal]],
    ) -> Decimal:
        if not bids or not asks:
            return Decimal("0")

        levels = self.config.orderbook_imbalance_levels
        bid_depth = sum(size for _price, size in sorted(bids, reverse=True)[:levels])
        ask_depth = sum(size for _price, size in sorted(asks)[:levels])
        total_depth = bid_depth + ask_depth
        if total_depth <= 0:
            return Decimal("0")
        return (bid_depth - ask_depth) / total_depth

    @staticmethod
    def _top_of_book_sizes(
        bids: list[tuple[Decimal, Decimal]],
        asks: list[tuple[Decimal, Decimal]],
    ) -> tuple[Decimal, Decimal]:
        best_bid_size = Decimal("0")
        best_ask_size = Decimal("0")
        if bids:
            _price, best_bid_size = max(bids, key=lambda level: level[0])
        if asks:
            _price, best_ask_size = min(asks, key=lambda level: level[0])
        return best_bid_size, best_ask_size

    def _calculate_limit_price(
        self,
        token: TokenConfig,
        side: str,
        predicted_mid: Decimal,
        quote: Quote,
        position: Position,
        market_conviction_yes: Decimal,
    ) -> Decimal:
        is_gtc = self.config.order_type == "GTC"
        
        # 计算仓位偏斜影响量 (Inventory Skew)
        # 仓位比例: 当前持仓 / 最大持仓
        inventory_ratio = position.notional / max(Decimal("1"), self.config.max_token_exposure_usdc)
        # 根据持仓调整预测的中价：持仓越多，报价越低 (偏向卖出)
        skew_adjust = inventory_ratio * self.config.inventory_skew_fraction * quote.mid
        # adjusted_fair = predicted_mid - skew_adjust

        # 2. --- 新增：方向性调整 (Directional Skew) ---
        directional_skew_adjust = Decimal("0")
        # 定义一个调整强度的系数，可以在 config.py 中配置
        # directional_skew_factor = Decimal("0.5") 
        directional_skew_factor = self.config.directional_skew_factor

        # 如果我们正在为 YES 代币定价
        if token.outcome == "YES":
            # 如果市场强烈看好 YES (价格 > 0.7)，我们就降低自己的买价（变得更挑剔），
            # 同时也会间接降低卖价（更想卖掉已有库存）。
            # (market_conviction_yes - 0.5) 是一个中性值为0的偏离度。
            directional_skew_adjust = (market_conviction_yes - Decimal("0.5")) * directional_skew_factor * quote.mid
        # 如果我们正在为 NO 代币定价
        elif token.outcome == "NO":
            # NO 的信念与 YES 相反
            market_conviction_no = Decimal("1") - market_conviction_yes
            # 如果市场强烈看好 NO (价格 > 0.7)，我们就降低自己的买价
            directional_skew_adjust = (market_conviction_no - Decimal("0.5")) * directional_skew_factor * quote.mid
        # 3. 组合所有调整
        adjusted_fair = predicted_mid - skew_adjust - directional_skew_adjust
        LOGGER.debug(
        "[DEBUG] %s %s PriceCalc: PredMid=%s, InvSkew=%s, DirSkew=%s -> AdjustedFair=%s",
        side, token.outcome, predicted_mid.quantize(Decimal("0.0001")),
        skew_adjust.quantize(Decimal("0.0001")),
        directional_skew_adjust.quantize(Decimal("0.0001")),
        adjusted_fair.quantize(Decimal("0.0001"))
    )


        if side == "BUY":
            if is_gtc:
                # Maker 模式：买价必须 < 卖一 (quote.ask)
                # 我们的目标价是预测中价与 (卖一 - 1 tick) 的最小值
                buffer = token.tick_size * 2
                target_price = min(adjusted_fair, quote.ask - buffer)
                # target_price = min(adjusted_fair, quote.ask - token.tick_size)
                # 同时，如果我们想做最佳买家，可以尝试不低于当前的买一
                raw_price = max(target_price, quote.bid) if target_price >= quote.bid else target_price
            else:
                raw_price = max(quote.ask, predicted_mid) * (
                    Decimal("1") + self.config.fok_price_buffer_fraction
                )
            price = min(self.config.max_price, max(self.config.min_price, raw_price))
            return self.adapter.round_price(price, token.tick_size, ROUND_DOWN if is_gtc else ROUND_UP)

        if is_gtc:
            # Maker 模式：卖价必须 > 买一 (quote.bid)
            target_price = max(adjusted_fair, quote.bid + token.tick_size)
            # 尝试不高于当前的卖一以获得更好的成交概率
            raw_price = min(target_price, quote.ask) if target_price <= quote.ask else target_price
        else:
            raw_price = min(quote.bid, predicted_mid) * (
                Decimal("1") - self.config.fok_price_buffer_fraction
            )
        price = min(self.config.max_price, max(self.config.min_price, raw_price))
        return self.adapter.round_price(price, token.tick_size, ROUND_UP)
    

    def _execute_order_with_retry(self, order: TargetOrder, max_retries: int = 3):
        """
        执行一个下单目标，并在遇到“余额不足”时自动降低金额并重试。
        """
        current_notional = self.dynamic_order_notional
        current_shares = order.shares
        current_price = order.price

        for attempt in range(max_retries):
            try:
                LOGGER.info(
                    "[TRADE] Attempt %d: Placing %s %s: Price=%s | Shares=%s | Notional=$%.2f",
                    attempt + 1, order.side, order.token.outcome, current_price,
                    current_shares, float(current_price * current_shares)
                )

                self.adapter.place_limit_order(
                    token=order.token,
                    side=order.side,
                    price=current_price,
                    size=current_shares,
                    dry_run=self.config.dry_run,
                )

                # 如果下单成功
                LOGGER.info("Successfully placed order.")
                # 记录成功时间，并返回
                self.last_success_order_time = time.time()
                return

            except PolyApiException as exc:
                msg = str(exc.error_msg).lower()
                # 检查是否是“余额不足”错误
                if "not enough balance" in msg:
                    LOGGER.warning(
                        "[RETRY] Attempt %d failed: Not enough balance. Reducing notional and retrying.",
                        attempt + 1
                    )
                    # 将下单金额降低10%
                    current_notional *= Decimal("0.9")
                    # 更新下单股数
                    new_shares = self._shares_for_notional(current_notional, current_price, order.token)

                    if new_shares is None:
                        LOGGER.error("Reduced notional is too small to place order. Aborting retry.")
                        # 将这个打折后的金额保存下来，以便下一轮使用
                        self.dynamic_order_notional = max(current_notional, Decimal("1.0")) # 避免降到0
                        break # 退出重试循环

                    current_shares = new_shares
                    # 短暂等待后重试
                    time.sleep(0.5) 
                    continue # 继续下一次循环尝试

                # 对于其他 post-only 错误，只记录不重试
                elif "post only" in msg or "order crosses book" in msg:
                    LOGGER.warning("[POST_ONLY] %s %s @ %s rejected (would cross book)", order.side, order.token.outcome, current_price)
                    break # 退出重试循环

                else:
                    # 对于其他所有 API 错误，直接抛出，让主循环捕获
                    LOGGER.error("[API_ERROR] Unhandled API error on order placement: %s", msg)
                    raise

        # 如果所有重试都失败了
        LOGGER.error(
            "Failed to place order for %s %s after %d retries.",
            order.side, order.token.outcome, max_retries
        )
        # 将最后一次尝试的、打折后的金额保存下来，作为下一轮的动态金额
        self.dynamic_order_notional = max(current_notional, Decimal("1.0"))
