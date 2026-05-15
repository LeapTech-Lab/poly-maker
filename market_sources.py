"""市场来源：.env、Google Sheet、或 rewards 自动发现。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pandas as pd

from config import BotConfig
from poly_utils.google_utils import get_spreadsheet


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketSpec:
    condition_id: str
    token_yes: str
    token_no: str
    question: str = ""
    end_date_iso: str = ""


def load_google_sheet_markets(config: BotConfig) -> list[MarketSpec]:
    """从 Google Sheet 读取市场。

    支持旧仓库表结构：`token1` / `token2` / `condition_id` / `question`。
    `Selected Markets` 如果只放 question，则会尝试和 `All Markets` 按 question 合并。
    """

    spreadsheet = get_spreadsheet(read_only=False)
    requested = config.google_sheet_worksheet

    df = _worksheet_df(spreadsheet, requested)
    if requested == "Selected Markets":
        df = _merge_selected_with_all(spreadsheet, df)

    specs = _df_to_specs(df)

    if not specs:
        raise RuntimeError(
            f"No usable markets found in Google Sheet worksheet={requested}. "
            "Need columns token1/token2/condition_id, or matching question rows between "
            "Selected Markets and All Markets. You can set GOOGLE_SHEET_WORKSHEET=All Markets "
            "or Volatility Markets explicitly."
        )

    limited = specs[: config.google_sheet_limit]
    LOGGER.info("Loaded %s market(s) from Google Sheet worksheet=%s", len(limited), requested)
    return limited


def _worksheet_df(spreadsheet: Any, title: str) -> pd.DataFrame:
    worksheet = spreadsheet.worksheet(title)
    df = pd.DataFrame(worksheet.get_all_records())
    if df.empty:
        return df
    if "question" in df.columns:
        df = df[df["question"].astype(str).str.strip() != ""]
    return df.reset_index(drop=True)


def _merge_selected_with_all(spreadsheet: Any, selected: pd.DataFrame) -> pd.DataFrame:
    if selected.empty or {"token1", "token2", "condition_id"}.issubset(selected.columns):
        return selected
    if "question" not in selected.columns:
        return selected

    all_markets = _worksheet_df(spreadsheet, "All Markets")
    if all_markets.empty or "question" not in all_markets.columns:
        return selected
    return selected.merge(all_markets, on="question", how="inner", suffixes=("", "_all"))


def _first(row: pd.Series, names: tuple[str, ...]) -> str:
    for name in names:
        if name in row and pd.notna(row[name]) and str(row[name]).strip():
            return str(row[name]).strip()
    return ""


def _df_to_specs(df: pd.DataFrame) -> list[MarketSpec]:
    specs: list[MarketSpec] = []
    if df.empty:
        return specs

    for _, row in df.iterrows():
        token_yes = _first(row, ("token1", "token_yes", "TOKEN_ID_YES", "yes_token_id", "YES_TOKEN_ID"))
        token_no = _first(row, ("token2", "token_no", "TOKEN_ID_NO", "no_token_id", "NO_TOKEN_ID"))
        condition_id = _first(row, ("condition_id", "CONDITION_ID", "conditionId"))
        question = _first(row, ("question", "title", "market_slug"))
        end_date_iso = _first(row, ("end_date_iso", "endDate", "end_date", "game_start_time"))
        if token_yes and token_no:
            specs.append(
                MarketSpec(
                    condition_id=condition_id,
                    token_yes=token_yes,
                    token_no=token_no,
                    question=question,
                    end_date_iso=end_date_iso,
                )
            )
    return specs
