"""轻量级 Polymarket 做市机器人入口。"""

from __future__ import annotations

import logging
import os
import time
from logging.handlers import RotatingFileHandler

from config import BotConfig
from market_sources import load_google_sheet_markets
from market_maker import MarketMaker
from polymarket_adapter import FatalTradingError, PolymarketAdapter


def setup_logging(log_file: str) -> None:
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5)
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)


def main() -> None:
    config = BotConfig()
    config.validate()
    setup_logging(config.log_file)
    logging.getLogger(__name__).info("Booting Polymarket lightweight market maker")
    adapter = PolymarketAdapter(config)

    if config.market_source == "google_sheet":
        specs = load_google_sheet_markets(config)
        makers = [MarketMaker(config, adapter, spec) for spec in specs]
        for maker in makers:
            maker.bootstrap()
        while True:
            try:
                for maker in makers:
                    maker.tick()
                time.sleep(config.refresh_interval_seconds)
            except KeyboardInterrupt:
                for maker in makers:
                    maker.shutdown()
                break
            except FatalTradingError:
                logging.getLogger(__name__).exception("Fatal trading error; stopping all makers")
                for maker in makers:
                    maker.shutdown()
                break
            except Exception:
                logging.getLogger(__name__).exception("Multi-market loop error; retrying")
                time.sleep(min(30, config.refresh_interval_seconds))
        return

    MarketMaker(config, adapter).run()


if __name__ == "__main__":
    main()
