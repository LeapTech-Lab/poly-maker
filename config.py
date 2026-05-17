"""轻量级 Polymarket 做市机器人配置。

所有参数都从环境变量读取，避免把私钥、地址或实盘参数写死在代码里。
默认值按约 100 USDC 小资金账户设计：单市场风险 25 美元，全局风险 80 美元。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from dotenv import load_dotenv


load_dotenv()


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def _decimal(name: str, default: str) -> Decimal:
    return Decimal(_env(name, default) or default)


def _int(name: str, default: str) -> int:
    return int(_env(name, default) or default)


def _bool(name: str, default: str = "false") -> bool:
    return (_env(name, default) or default).strip().lower() in {"1", "true", "yes", "y"}


@dataclass(frozen=True)
class BotConfig:
    """机器人运行参数。

    token_yes/token_no 优先；如果只给 condition_id，会自动从 CLOB market info 解析 token。
    price_spread_bps 是旧报价参数；当前模式使用预测价格进行挂单。
    主模式使用 GTC Post-only 预测限价单，通过 Post-only 确保只做 Maker，不产生 Taker 手续费。
    order_notional_usdc 是每个订单希望投入的美元金额，代码会换算为 shares。
    """

    host: str = _env("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com") or ""
    chain_id: int = _int("CHAIN_ID", "137")
    private_key: str = _env("PK", "") or ""
    funder_address: str = _env("FUNDER_ADDRESS", _env("BROWSER_ADDRESS", "")) or ""
    signature_type: int = _int("SIGNATURE_TYPE", "2")
    clob_api_key: str = _env("CLOB_API_KEY", "") or ""
    clob_secret: str = _env("CLOB_SECRET", "") or ""
    clob_passphrase: str = _env("CLOB_PASS_PHRASE", "") or ""

    condition_id: str = _env("CONDITION_ID", "") or ""
    token_yes: str = _env("TOKEN_ID_YES", "") or ""
    token_no: str = _env("TOKEN_ID_NO", "") or ""
    market_source: str = _env("MARKET_SOURCE", "env") or "env"
    google_sheet_worksheet: str = _env("GOOGLE_SHEET_WORKSHEET", "Selected Markets") or "Selected Markets"
    google_sheet_limit: int = _int("GOOGLE_SHEET_LIMIT", "5")
    auto_select_reward_market: bool = _bool("AUTO_SELECT_REWARD_MARKET", "false")
    prefer_sports: bool = _bool("PREFER_SPORTS", "true")

    price_spread_bps: Decimal = _decimal("PRICE_SPREAD_BPS", "120")
    order_notional_usdc: Decimal = _decimal("ORDER_NOTIONAL_USDC", "5")
    refresh_interval_seconds: int = _int("REFRESH_INTERVAL_SECONDS", "8")
    min_price: Decimal = _decimal("MIN_PRICE", "0.03")
    max_price: Decimal = _decimal("MAX_PRICE", "0.97")

    max_market_exposure_usdc: Decimal = _decimal("MAX_MARKET_EXPOSURE_USDC", "25")
    max_token_exposure_usdc: Decimal = _decimal("MAX_TOKEN_EXPOSURE_USDC", "30")
    max_global_exposure_usdc: Decimal = _decimal("MAX_GLOBAL_EXPOSURE_USDC", "80")
    count_existing_positions_in_global_limit: bool = _bool("COUNT_EXISTING_POSITIONS_IN_GLOBAL_LIMIT", "false")
    inventory_skew_threshold_usdc: Decimal = _decimal("INVENTORY_SKEW_THRESHOLD_USDC", "18")
    inventory_skew_bps: Decimal = _decimal("INVENTORY_SKEW_BPS", "60")
    stop_loss_pct: Decimal = _decimal("STOP_LOSS_PCT", "12")
    take_profit_pct: Decimal = _decimal("TAKE_PROFIT_PCT", "8")
    max_market_loss_usdc: Decimal = _decimal("MAX_MARKET_LOSS_USDC", "8")
    max_midpoint_move_bps: Decimal = _decimal("MAX_MIDPOINT_MOVE_BPS", "350")
    close_only_hours_before_end: int = _int("CLOSE_ONLY_HOURS_BEFORE_END", "24")
    risk_off_after_stop: bool = _bool("RISK_OFF_AFTER_STOP", "true")

    cancel_on_start: bool = _bool("CANCEL_ON_START", "true")
    order_type: str = (_env("ORDER_TYPE", "GTC") or "GTC").upper()
    post_only: bool = _bool("POST_ONLY", "true")
    fok_price_buffer_bps: Decimal = _decimal("FOK_PRICE_BUFFER_BPS", "5")
    fok_min_edge_bps: Decimal = _decimal("FOK_MIN_EDGE_BPS", "1")
    cancel_unfilled_after_ms: int = _int("CANCEL_UNFILLED_AFTER_MS", "0")
    prediction_latency_ms: int = _int("PREDICTION_LATENCY_MS", "0")
    prediction_edge_bps: Decimal = _decimal("PREDICTION_EDGE_BPS", "8")
    min_prediction_confidence: Decimal = _decimal("MIN_PREDICTION_CONFIDENCE", "0.10")
    orderbook_imbalance_levels: int = _int("ORDERBOOK_IMBALANCE_LEVELS", "3")
    dry_run: bool = _bool("DRY_RUN", "true")
    log_file: str = _env("LOG_FILE", "logs/market_maker.log") or "logs/market_maker.log"

    @property
    def spread_fraction(self) -> Decimal:
        return self.price_spread_bps / Decimal("10000")

    @property
    def inventory_skew_fraction(self) -> Decimal:
        return self.inventory_skew_bps / Decimal("10000")

    @property
    def stop_loss_fraction(self) -> Decimal:
        return self.stop_loss_pct / Decimal("100")

    @property
    def take_profit_fraction(self) -> Decimal:
        return self.take_profit_pct / Decimal("100")

    @property
    def max_midpoint_move_fraction(self) -> Decimal:
        return self.max_midpoint_move_bps / Decimal("10000")

    @property
    def fok_price_buffer_fraction(self) -> Decimal:
        return self.fok_price_buffer_bps / Decimal("10000")

    @property
    def fok_min_edge_fraction(self) -> Decimal:
        return self.fok_min_edge_bps / Decimal("10000")

    def validate(self) -> None:
        if not self.private_key and not self.dry_run:
            raise ValueError("PK is required when DRY_RUN=false")
        if self.refresh_interval_seconds < 1:
            LOGGER.warning("High frequency trading enabled (REFRESH_INTERVAL_SECONDS < 1)")
        if self.order_notional_usdc <= 0:
            raise ValueError("ORDER_NOTIONAL_USDC must be positive")
        if self.order_type not in {"FOK", "GTC"}:
            raise ValueError("ORDER_TYPE must be FOK or GTC")
        if self.fok_price_buffer_bps < 0:
            raise ValueError("FOK_PRICE_BUFFER_BPS must be >= 0")
        if self.fok_min_edge_bps < 0:
            raise ValueError("FOK_MIN_EDGE_BPS must be >= 0")
        if self.cancel_unfilled_after_ms < 0:
            raise ValueError("CANCEL_UNFILLED_AFTER_MS must be >= 0")
        if self.prediction_latency_ms < 0:
            raise ValueError("PREDICTION_LATENCY_MS must be >= 0")
        if self.orderbook_imbalance_levels <= 0:
            raise ValueError("ORDERBOOK_IMBALANCE_LEVELS must be positive")
        if self.max_market_exposure_usdc > Decimal("30"):
            raise ValueError("MAX_MARKET_EXPOSURE_USDC should stay <= 30 for the small-cap profile")
        if self.max_global_exposure_usdc > Decimal("100"):
            raise ValueError("MAX_GLOBAL_EXPOSURE_USDC should stay <= total bankroll")
        has_tokens = bool(self.token_yes and self.token_no)
        if self.market_source not in {"env", "google_sheet", "auto_rewards"}:
            raise ValueError("MARKET_SOURCE must be env, google_sheet, or auto_rewards")
        if self.market_source == "google_sheet":
            if self.google_sheet_limit <= 0:
                raise ValueError("GOOGLE_SHEET_LIMIT must be positive")
            return
        if self.market_source == "auto_rewards" or self.auto_select_reward_market:
            return
        if not has_tokens and not self.condition_id:
            raise ValueError(
                "Set TOKEN_ID_YES + TOKEN_ID_NO, CONDITION_ID, MARKET_SOURCE=google_sheet, or MARKET_SOURCE=auto_rewards"
            )
