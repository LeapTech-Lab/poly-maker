"""从 Polymarket 网页 URL / slug / condition_id 解析交易所需 ID。

示例：
    uv run python resolve_market.py "https://polymarket.com/event/.../market-slug"
    uv run python resolve_market.py market-slug
    uv run python resolve_market.py 0xabc...
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Any
from urllib.parse import urlparse

import requests
from py_clob_client_v2 import ClobClient


GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def normalize_slug(value: str) -> str:
    value = value.strip()
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise ValueError("URL path is empty")
        return parts[-1]
    return value.strip("/")


def get_json(url: str) -> Any:
    response = requests.get(url, timeout=20)
    if response.status_code == 404:
        return {}
    response.raise_for_status()
    return response.json()


def parse_jsonish(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except json.JSONDecodeError:
            return [value]
    return [str(value)]


def market_from_slug(slug: str, index: int | None = None, contains: str = "") -> dict[str, Any]:
    # 单市场 URL 通常最后一段就是 market slug。
    direct = get_json(f"{GAMMA}/markets/slug/{slug}")
    if isinstance(direct, dict) and direct:
        if direct.get("type") == "not found error":
            direct = {}
        else:
            return direct

    # 有些网页 URL 最后一段可能是 event slug，兜底查 event 下的 markets。
    event = get_json(f"{GAMMA}/events/slug/{slug}")
    markets = event.get("markets", []) if isinstance(event, dict) else []
    if markets:
        if contains:
            wanted = contains.lower()
            matches = [
                market for market in markets
                if wanted in str(market.get("question") or market.get("title") or market.get("slug") or "").lower()
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                print_event_markets(matches, prefix=f"匹配到多个包含 {contains!r} 的子市场")
                raise RuntimeError("请用 --index 指定其中一个编号")
            print_event_markets(markets, prefix=f"没有找到包含 {contains!r} 的子市场")
            raise RuntimeError("请换一个 --contains 关键词")

        if index is not None:
            if index < 0 or index >= len(markets):
                print_event_markets(markets)
                raise RuntimeError(f"--index 超出范围：0 到 {len(markets) - 1}")
            return markets[index]

        if len(markets) > 1:
            print_event_markets(markets)
            raise RuntimeError("这是 event 页面，请加 --index N 或 --contains 'December 31' 选择具体子市场")
        return markets[0]
    raise RuntimeError(f"No market found for slug={slug}")


def print_event_markets(markets: list[dict[str, Any]], prefix: str = "这个 event 下有多个子市场") -> None:
    print(prefix + "：")
    for idx, market in enumerate(markets):
        question = market.get("question") or market.get("title") or market.get("slug") or ""
        condition_id = market.get("condition_id") or market.get("conditionId") or ""
        print(f"[{idx}] {question}")
        print(f"    slug={market.get('slug')}")
        print(f"    condition_id={condition_id}")


def market_from_condition_id(condition_id: str) -> dict[str, Any]:
    client = ClobClient(host=CLOB, chain_id=137)
    return client.get_market(condition_id)


def extract_market(value: str, index: int | None = None, contains: str = "") -> dict[str, Any]:
    if re.fullmatch(r"0x[a-fA-F0-9]{64}", value.strip()):
        return market_from_condition_id(value.strip())
    return market_from_slug(normalize_slug(value), index=index, contains=contains)


def print_env(market: dict[str, Any]) -> None:
    question = market.get("question") or market.get("title") or market.get("slug") or ""
    condition_id = market.get("condition_id") or market.get("conditionId") or ""
    outcomes = parse_jsonish(market.get("outcomes"))
    token_ids = parse_jsonish(market.get("clobTokenIds"))

    if not token_ids and market.get("tokens"):
        for token in market["tokens"]:
            token_ids.append(str(token.get("token_id") or token.get("tokenId") or token.get("id")))
            outcomes.append(str(token.get("outcome") or token.get("name") or ""))

    print(f"QUESTION={question}")
    print(f"CONDITION_ID={condition_id}")
    if len(token_ids) >= 1:
        print(f"TOKEN_ID_YES={token_ids[0]}")
    if len(token_ids) >= 2:
        print(f"TOKEN_ID_NO={token_ids[1]}")
    if outcomes:
        print(f"OUTCOMES={outcomes}")
    print(f"ACTIVE={market.get('active')}")
    print(f"CLOSED={market.get('closed')}")
    print(f"ACCEPTING_ORDERS={market.get('accepting_orders') or market.get('acceptingOrders')}")
    print(f"BEST_BID={market.get('bestBid') or market.get('best_bid')}")
    print(f"BEST_ASK={market.get('bestAsk') or market.get('best_ask')}")
    print(f"LIQUIDITY={market.get('liquidityNum') or market.get('liquidity')}")
    print(f"VOLUME_24H={market.get('volume24hr') or market.get('volume24hrClob')}")
    print(f"REWARDS_MIN_SIZE={market.get('rewardsMinSize') or market.get('rewards_min_size')}")
    print(f"REWARDS_MAX_SPREAD={market.get('rewardsMaxSpread') or market.get('rewards_max_spread')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve Polymarket URL/slug/condition_id to token IDs")
    parser.add_argument("market", help="Polymarket URL, slug, or condition_id")
    parser.add_argument("--index", type=int, default=None, help="event 页面有多个子市场时，选择第几个，从 0 开始")
    parser.add_argument("--contains", default="", help="event 页面有多个子市场时，按问题文本筛选，例如 'December 31'")
    args = parser.parse_args()
    print_env(extract_market(args.market, index=args.index, contains=args.contains))


if __name__ == "__main__":
    main()
