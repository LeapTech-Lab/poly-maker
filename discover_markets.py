"""列出 Polymarket 当前有 rewards 的市场，辅助填写 .env。

这个脚本只读公开 CLOB 数据，不需要私钥。用法：

    uv run python discover_markets.py --limit 20
    uv run python discover_markets.py --condition-id 0x...
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from dotenv import load_dotenv
from py_clob_client_v2 import ClobClient


load_dotenv()


def _text(item: dict[str, Any]) -> str:
    parts = []
    for key in ("question", "title", "slug", "market_slug", "event_slug"):
        value = item.get(key)
        if value:
            parts.append(str(value))
    return " | ".join(parts) or "<no title>"


def _condition_id(item: dict[str, Any]) -> str:
    return str(item.get("condition_id") or item.get("conditionId") or item.get("market") or "")


def _tokens_from_market(market: dict[str, Any]) -> list[tuple[str, str]]:
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
            tokens.append((str(outcome), str(token_id)))
    return tokens


def print_market(client: ClobClient, condition_id: str, title: str = "") -> None:
    market = client.get_market(condition_id)
    if not market:
        market = client.get_clob_market_info(condition_id)
    question = title or market.get("question") or market.get("title") or market.get("market_slug") or ""
    print("=" * 88)
    print(f"QUESTION: {question}")
    print(f"CONDITION_ID={condition_id}")
    for outcome, token_id in _tokens_from_market(market):
        print(f"{outcome.upper()}_TOKEN_ID={token_id}")
    print(f"tick_size={market.get('minimum_tick_size') or market.get('tick_size') or market.get('tickSize') or market.get('mts')}")
    print(f"neg_risk={market.get('neg_risk') or market.get('negRisk') or market.get('nr')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover Polymarket rewards markets")
    parser.add_argument("--limit", type=int, default=20, help="最多打印多少个 rewards 市场")
    parser.add_argument("--sports-only", action="store_true", help="只显示标题中像体育的市场")
    parser.add_argument("--with-titles", action="store_true", help="逐个查询 market 详情，打印真实标题")
    parser.add_argument("--condition-id", default="", help="查看某个 condition_id 的 token ids")
    args = parser.parse_args()

    host = os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
    chain_id = int(os.getenv("CHAIN_ID", "137"))
    client = ClobClient(host=host, chain_id=chain_id)

    if args.condition_id:
        print_market(client, args.condition_id)
        return

    sports_words = ("nba", "nfl", "mlb", "nhl", "ufc", "soccer", "football", "tennis")
    shown = 0
    for item in client.get_current_rewards():
        title = _text(item)
        condition_id = _condition_id(item)
        if not condition_id:
            continue
        try:
            market = None
            full_title = title
            if args.with_titles or args.sports_only:
                market = client.get_market(condition_id)
                if isinstance(market, dict):
                    full_title = (
                        market.get("question")
                        or market.get("title")
                        or market.get("market_slug")
                        or title
                    )
            if args.sports_only and not any(word in str(full_title).lower() for word in sports_words):
                continue
            print_market(client, condition_id, str(full_title))
            shown += 1
        except Exception as exc:
            print(f"skip condition_id={condition_id}: {exc}")
        if shown >= args.limit:
            break


if __name__ == "__main__":
    main()
