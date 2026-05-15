"""Polymarket CLOB v2 SDK 适配层。

这里把 SDK 返回值、orderbook 结构、余额/持仓查询都收敛成机器人内部更稳定的格式。
实盘相关方法集中在这里，后续迁移 SDK 或增加 WebSocket 时影响范围更小。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Iterable, Optional

import requests
from py_clob_client_v2 import (
    ApiCreds,
    ClobClient,
    OpenOrderParams,
    OrderArgs,
    OrderMarketCancelParams,
    OrderType,
    PartialCreateOrderOptions,
    Side,
)
from py_clob_client_v2.exceptions import PolyApiException

from config import BotConfig
from market_sources import MarketSpec


LOGGER = logging.getLogger(__name__)


class FatalTradingError(RuntimeError):
    """不可通过普通重试恢复的交易错误。"""


@dataclass(frozen=True)
class TokenConfig:
    token_id: str
    outcome: str
    tick_size: Decimal
    neg_risk: bool
    min_order_size: Decimal = Decimal("5")
    end_date_iso: str = ""


@dataclass(frozen=True)
class Quote:
    bid: Decimal
    ask: Decimal
    mid: Decimal


@dataclass(frozen=True)
class Position:
    token_id: str
    size: Decimal
    avg_price: Decimal
    current_price: Decimal

    @property
    def notional(self) -> Decimal:
        return self.size * self.current_price

    @property
    def unrealized_pnl(self) -> Decimal:
        if self.size <= 0:
            return Decimal("0")
        return (self.current_price - self.avg_price) * self.size


class PolymarketAdapter:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        creds = None
        if config.clob_api_key and config.clob_secret and config.clob_passphrase:
            creds = ApiCreds(
                api_key=config.clob_api_key,
                api_secret=config.clob_secret,
                api_passphrase=config.clob_passphrase,
            )

        self.client = ClobClient(
            host=config.host,
            chain_id=config.chain_id,
            key=config.private_key or None,
            creds=creds,
            signature_type=config.signature_type,
            funder=config.funder_address or None,
            retry_on_error=True,
        )

        if config.private_key and creds is None and not config.dry_run:
            LOGGER.info("Deriving CLOB API credentials from wallet signature")
            self.client.set_api_creds(self.client.create_or_derive_api_key())

        self.address = self.client.get_address() if config.private_key else ""

    def resolve_tokens(self) -> tuple[TokenConfig, TokenConfig, str]:
        if self.config.token_yes and self.config.token_no:
            end_date_iso = ""
            if self.config.condition_id:
                try:
                    market = self.client.get_market(self.config.condition_id)
                    end_date_iso = str(market.get("end_date_iso") or market.get("game_start_time") or "")
                except Exception as exc:
                    LOGGER.debug("Could not fetch market end date: %s", exc)
            yes = self._token_config(self.config.token_yes, "YES", end_date_iso=end_date_iso)
            no = self._token_config(self.config.token_no, "NO", end_date_iso=end_date_iso)
            return yes, no, self.config.condition_id

        condition_id = self.config.condition_id
        if not condition_id and (self.config.auto_select_reward_market or self.config.market_source == "auto_rewards"):
            condition_id = self.find_reward_market_condition_id()

        market = self.client.get_clob_market_info(condition_id)
        tokens = self._extract_tokens_from_market(market)
        if len(tokens) < 2:
            raise RuntimeError(f"Could not parse two outcome tokens from market {condition_id}")

        end_date_iso = str(market.get("end_date_iso") or market.get("game_start_time") or "")
        yes = self._token_config(tokens[0][0], tokens[0][1] or "YES", end_date_iso=end_date_iso)
        no = self._token_config(tokens[1][0], tokens[1][1] or "NO", end_date_iso=end_date_iso)
        LOGGER.info("Resolved market condition_id=%s yes=%s no=%s", condition_id, yes.token_id, no.token_id)
        return yes, no, condition_id

    def resolve_market_spec(self, spec: MarketSpec) -> tuple[TokenConfig, TokenConfig, str]:
        yes = self._token_config(spec.token_yes, "YES", end_date_iso=spec.end_date_iso)
        no = self._token_config(spec.token_no, "NO", end_date_iso=spec.end_date_iso)
        LOGGER.info(
            "Resolved sheet market condition_id=%s yes=%s no=%s question=%s",
            spec.condition_id or "<token-only>",
            yes.token_id,
            no.token_id,
            spec.question,
        )
        return yes, no, spec.condition_id

    def find_reward_market_condition_id(self) -> str:
        rewards = self.client.get_current_rewards()
        sports_words = ("nba", "nfl", "mlb", "nhl", "ufc", "soccer", "football", "tennis")
        candidates = []
        for item in rewards:
            text = " ".join(str(item.get(k, "")) for k in ("question", "title", "slug", "market_slug"))
            if self.config.prefer_sports and not any(word in text.lower() for word in sports_words):
                continue
            condition_id = item.get("condition_id") or item.get("conditionId") or item.get("market")
            if condition_id:
                candidates.append((condition_id, text))
        if not candidates:
            raise RuntimeError("No rewards market found. Set CONDITION_ID or TOKEN_ID_YES/TOKEN_ID_NO manually.")
        LOGGER.info("Auto-selected rewards market: %s (%s)", candidates[0][0], candidates[0][1])
        return str(candidates[0][0])

    def get_quote(self, token: TokenConfig) -> Quote:
        book = self.client.get_order_book(token.token_id)
        bids = self._levels(book, "bids")
        asks = self._levels(book, "asks")
        if not bids or not asks:
            mid_resp = self.client.get_midpoint(token.token_id)
            mid = Decimal(str(mid_resp["mid"] if isinstance(mid_resp, dict) else mid_resp))
            return Quote(bid=mid, ask=mid, mid=mid)
        best_bid = max(price for price, _ in bids)
        best_ask = min(price for price, _ in asks)
        return Quote(bid=best_bid, ask=best_ask, mid=(best_bid + best_ask) / Decimal("2"))

    def get_positions(
        self,
        tokens: Iterable[TokenConfig],
        quotes: Optional[dict[str, Quote]] = None,
    ) -> dict[str, Position]:
        tracked = {token.token_id: token for token in tokens}
        rows = self._data_api_positions()
        positions: dict[str, Position] = {}
        for token in tracked.values():
            current = quotes[token.token_id].mid if quotes and token.token_id in quotes else self.get_quote(token).mid
            positions[token.token_id] = Position(token.token_id, Decimal("0"), Decimal("0"), current)

        for row in rows:
            token_id = str(row.get("asset") or row.get("asset_id") or row.get("token_id") or "")
            if token_id not in tracked:
                continue
            size = Decimal(str(row.get("size", "0")))
            avg_price = Decimal(str(row.get("avgPrice") or row.get("avg_price") or "0"))
            current = positions[token_id].current_price
            positions[token_id] = Position(token_id, size, avg_price, current)
        return positions

    def get_global_exposure(self) -> Decimal:
        return sum(position.notional for position in self.get_all_positions())

    def get_all_positions(self) -> list[Position]:
        positions = []
        for row in self._data_api_positions():
            token_id = str(row.get("asset") or row.get("asset_id") or row.get("token_id") or "")
            if not token_id:
                continue
            size = Decimal(str(row.get("size", "0")))
            avg_price = Decimal(str(row.get("avgPrice") or row.get("avg_price") or "0"))
            current = Decimal(str(row.get("curPrice") or row.get("currentValue") or row.get("price") or avg_price))
            positions.append(Position(token_id, size, avg_price, current))
        return positions

    def cancel_market_orders(self, condition_id: str) -> None:
        if self.config.dry_run:
            LOGGER.info("[DRY_RUN] Would cancel old orders for condition_id=%s", condition_id)
            return
        LOGGER.info("Cancelling old orders for condition_id=%s", condition_id)
        self.client.cancel_market_orders(OrderMarketCancelParams(market=condition_id))

    def cancel_token_orders(self, token_id: str) -> None:
        if self.config.dry_run:
            LOGGER.info("[DRY_RUN] Would cancel old orders for token_id=%s", token_id)
            return
        LOGGER.info("Cancelling old orders for token_id=%s", token_id)
        self.client.cancel_market_orders(OrderMarketCancelParams(asset_id=token_id))

    def open_orders_for_token(self, token_id: str) -> list[dict[str, Any]]:
        return self.client.get_open_orders(OpenOrderParams(asset_id=token_id))

    def place_limit_order(
        self,
        token: TokenConfig,
        side: str,
        price: Decimal,
        size: Decimal,
        dry_run: bool,
    ) -> Optional[dict[str, Any]]:
        price = self.round_price(price, token.tick_size, ROUND_DOWN if side == "BUY" else ROUND_UP)
        if dry_run:
            LOGGER.info("[DRY_RUN] %s %s shares=%s price=%s", side, token.outcome, size, price)
            return None

        try:
            response = self.client.create_and_post_order(
                order_args=OrderArgs(
                    token_id=token.token_id,
                    price=float(price),
                    side=Side.BUY if side == "BUY" else Side.SELL,
                    size=float(size),
                ),
                options=PartialCreateOrderOptions(tick_size=str(token.tick_size), neg_risk=token.neg_risk),
                order_type=OrderType.GTC,
                post_only=self.config.post_only,
            )
        except PolyApiException as exc:
            msg = str(exc.error_msg).lower()
            if "invalid signature" in msg or "invalid funder" in msg:
                raise FatalTradingError(
                    "Order signing failed. Check PK, SIGNATURE_TYPE, and FUNDER_ADDRESS."
                ) from exc
            raise
        LOGGER.info("Posted %s %s shares=%s price=%s response=%s", side, token.outcome, size, price, response)
        return response

    @staticmethod
    def round_price(value: Decimal, tick_size: Decimal, rounding: str) -> Decimal:
        ticks = (value / tick_size).to_integral_value(rounding=rounding)
        return ticks * tick_size

    def today_rewards_estimate(self, condition_id: str) -> Decimal:
        if not self.address:
            return Decimal("0")
        today = dt.datetime.utcnow().strftime("%Y-%m-%d")
        try:
            result = self.client.get_total_earnings_for_user_for_day(today)
        except Exception as exc:
            LOGGER.debug("Reward estimate unavailable: %s", exc)
            return Decimal("0")
        if isinstance(result, dict):
            value = result.get("total") or result.get("earnings") or result.get("amount") or "0"
            return Decimal(str(value))
        return Decimal(str(result or "0"))

    def _token_config(self, token_id: str, outcome: str, end_date_iso: str = "") -> TokenConfig:
        book = self.client.get_order_book(str(token_id))
        min_order_size = Decimal(str(getattr(book, "min_order_size", "5") or "5"))
        return TokenConfig(
            token_id=str(token_id),
            outcome=outcome,
            tick_size=Decimal(str(self.client.get_tick_size(str(token_id)))),
            neg_risk=bool(self.client.get_neg_risk(str(token_id))),
            min_order_size=min_order_size,
            end_date_iso=end_date_iso,
        )

    @staticmethod
    def _extract_tokens_from_market(market: dict[str, Any]) -> list[tuple[str, str]]:
        raw_tokens = market.get("tokens") or market.get("clobTokenIds") or market.get("outcomes") or []
        tokens: list[tuple[str, str]] = []
        for idx, item in enumerate(raw_tokens):
            if isinstance(item, dict):
                token_id = item.get("token_id") or item.get("tokenId") or item.get("id") or item.get("t")
                outcome = item.get("outcome") or item.get("name") or item.get("o") or ("YES" if idx == 0 else "NO")
            else:
                token_id = item
                outcome = "YES" if idx == 0 else "NO"
            if token_id:
                tokens.append((str(token_id), str(outcome)))
        return tokens

    @staticmethod
    def _levels(book: Any, name: str) -> list[tuple[Decimal, Decimal]]:
        rows = book.get(name, []) if isinstance(book, dict) else getattr(book, name, [])
        levels = []
        for row in rows or []:
            price = row.get("price") if isinstance(row, dict) else getattr(row, "price", None)
            size = row.get("size") if isinstance(row, dict) else getattr(row, "size", None)
            if price is not None and size is not None:
                levels.append((Decimal(str(price)), Decimal(str(size))))
        return levels

    def _data_api_positions(self) -> list[dict[str, Any]]:
        if not self.config.funder_address:
            return []
        response = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": self.config.funder_address},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, list) else data.get("data", [])
