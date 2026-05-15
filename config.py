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
    price_spread_bps 是离 midpoint 的单边距离，例如 120 = 1.2%。
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

    cancel_on_start: bool = _bool("CANCEL_ON_START", "true")
    post_only: bool = _bool("POST_ONLY", "true")
    dry_run: bool = _bool("DRY_RUN", "true")
    log_file: str = _env("LOG_FILE", "logs/market_maker.log") or "logs/market_maker.log"

    @property
    def spread_fraction(self) -> Decimal:
        return self.price_spread_bps / Decimal("10000")

    @property
    def inventory_skew_fraction(self) -> Decimal:
        return self.inventory_skew_bps / Decimal("10000")

    def validate(self) -> None:
        if not self.private_key and not self.dry_run:
            raise ValueError("PK is required when DRY_RUN=false")
        if self.refresh_interval_seconds < 3:
            raise ValueError("REFRESH_INTERVAL_SECONDS should be >= 3 to reduce API pressure")
        if self.order_notional_usdc <= 0:
            raise ValueError("ORDER_NOTIONAL_USDC must be positive")
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
