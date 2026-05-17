# Poly-Maker

Poly-Maker 现在包含两套思路：

- 原仓库逻辑：复杂版 Polymarket 做市/统计工具，依赖 Google Sheets、WebSocket、仓位合并脚本。
- 新增轻量版：适合约 100 USDC 起步的小资金 Polymarket Market Making Bot，使用官方 `py-clob-client-v2`，通过 `.env` 配置，默认 `DRY_RUN=true`。

> 风险提醒：这是真金白银交易代码。做市可能因为盘口跳变、单边成交、结算事件、API/网络异常、奖励规则变化而亏损。先 dry run，再用极小订单实盘验证。

## 现有代码逻辑

原项目主要文件：

- `main.py`：现在是轻量版机器人入口。
- `config.py`：从 `.env` 读取交易、风控、市场选择参数。
- `polymarket_adapter.py`：封装 `py-clob-client-v2`，处理 token 解析、orderbook、持仓、撤单、FOK limit order。
- `market_maker.py`：核心执行循环，基于短期预测价发送 FOK 激进限价单，预测正确就全成交，预测错误或流动性不足就整单取消。
- `predictors.py`：短期预测接口。默认实现用 orderbook imbalance 预测 `PREDICTION_LATENCY_MS` 后的 midpoint，后续可以替换成因子栈或模型。
- `latency_probe.py`：延迟测量工具，用来估算“收到行情 -> 生成/提交订单”的预测窗口。
- `poly_data/`：原仓库的数据、WebSocket、订单簿和旧版客户端封装。
- `trading.py`：原仓库的复杂交易逻辑，基于 Google Sheets 参数、盘口深度、止损/止盈和仓位合并。
- `update_markets.py` / `update_stats.py`：更新市场列表和账户统计。
- `poly_merger/`：Node.js position merge 工具。

原版流程大致是：从 Google Sheets 拉市场和超参，维护全局 `global_state`，用 WebSocket 更新 orderbook/user 事件，再由 `trading.py` 针对每个 condition 做报价、止损、止盈和仓位合并。它更重，也更依赖表格配置。

轻量版流程是：

1. 启动时读取 `.env`。
2. 解析 `TOKEN_ID_YES`/`TOKEN_ID_NO`，或用 `CONDITION_ID` 自动拿两个 outcome token。
3. 启动时撤掉该市场旧订单。
4. 每 `REFRESH_INTERVAL_SECONDS` 秒刷新 midpoint、持仓和风险。
5. 按 `PREDICTION_LATENCY_MS` 指定的时间窗口预测短期 midpoint。
6. BUY 只有在预测 midpoint 高于当前 ask 的最小 edge 时触发，SELL 只有在预测 midpoint 低于当前 bid 的最小 edge 时触发。
7. 订单使用 `ORDER_TYPE=FOK`，限价会在当前可成交价附近加 buffer：预测正确且流动性足够就立刻全成交，否则整单取消。
8. FOK 不留下挂单，因此正常情况下不需要后续撤单。
9. 如果单 token 或单市场 exposure 超限，停止增加风险。
10. 如果某边 inventory 偏大，就把该边 bid 再压低，减少继续买入概率。
11. 每轮记录 market exposure、global exposure、未实现 PnL、预测和奖励估算。

## 安装

```bash
uv sync
```

如果还要运行原仓库的 `poly_merger`：

```bash
cd poly_merger
npm install
cd ..
```

## 配置

```bash
cp .env.example .env
```

如果你还不知道 `CONDITION_ID` 或 token id，先列出当前有 Liquidity Rewards 的市场：

```bash
uv run python discover_markets.py --limit 20
```

只看标题里像体育的 rewards 市场：

```bash
uv run python discover_markets.py --sports-only --limit 20
```

看到想做的市场后，把脚本输出的 `CONDITION_ID`、`YES_TOKEN_ID`、`NO_TOKEN_ID` 填进 `.env`。

如果你已经在网页上打开了某个市场，直接复制浏览器地址栏 URL：

```bash
uv run python resolve_market.py "https://polymarket.com/event/.../market-slug"
```

脚本会输出：

```env
CONDITION_ID=...
TOKEN_ID_YES=...
TOKEN_ID_NO=...
```

如果复制的是 event 页面，里面可能有多个子市场，可以先列出来再选编号：

```bash
uv run python resolve_market.py "https://polymarket.com/event/event-slug" --list
uv run python resolve_market.py "https://polymarket.com/event/event-slug" --index 0
```

也可以按问题文本筛选：

```bash
uv run python resolve_market.py "https://polymarket.com/event/event-slug" --contains "December 31"
```

也可以直接从 Google Sheet 读取市场：

```env
MARKET_SOURCE=google_sheet
GOOGLE_SHEET_WORKSHEET=Selected Markets
GOOGLE_SHEET_LIMIT=5
SPREADSHEET_URL=https://docs.google.com/spreadsheets/d/...
```

`GOOGLE_SHEET_WORKSHEET` 可以填 `Selected Markets`、`All Markets` 或 `Volatility Markets`。表里需要能读到 `token1`、`token2`、`condition_id`；如果 `Selected Markets` 只有 `question`，代码会自动按 `question` 去 `All Markets` 合并。

安全起见，`Selected Markets` 为空时不会自动交易 `All Markets`。如果你确实想从全量表取前 N 个市场，需要显式设置：

```env
GOOGLE_SHEET_WORKSHEET=All Markets
GOOGLE_SHEET_LIMIT=5
```

最小配置：

```env
PK=你的私钥
FUNDER_ADDRESS=你的 Polymarket funder/proxy wallet
SIGNATURE_TYPE=2

CONDITION_ID=目标市场 condition_id
# 或者更推荐显式指定：
TOKEN_ID_YES=YES_TOKEN_ID
TOKEN_ID_NO=NO_TOKEN_ID

DRY_RUN=true
ORDER_NOTIONAL_USDC=5
PRICE_SPREAD_BPS=120
REFRESH_INTERVAL_SECONDS=8
MAX_MARKET_EXPOSURE_USDC=25
MAX_GLOBAL_EXPOSURE_USDC=80
ORDER_TYPE=FOK
POST_ONLY=false
FOK_PRICE_BUFFER_BPS=5
FOK_MIN_EDGE_BPS=1
CANCEL_UNFILLED_AFTER_MS=0
PREDICTION_LATENCY_MS=0
PREDICTION_EDGE_BPS=8
MIN_PREDICTION_CONFIDENCE=0.10
ORDERBOOK_IMBALANCE_LEVELS=3
```

参数说明：

- `DRY_RUN=true`：只打印拟挂单，不真的发单。第一次必须先 dry run。
- `ORDER_NOTIONAL_USDC`：每笔订单用美元金额控制，例如 5 USDC。
- Polymarket 有按 shares 计的最小订单量，bot 会读取 `min_order_size`；如果你的美元订单太小导致 shares 不够，会跳过该边报价。
- `PRICE_SPREAD_BPS`：旧报价参数；当前 FOK 主方案不依赖它。
- `MAX_MARKET_EXPOSURE_USDC`：单市场最大风险，建议 20-30。
- `MAX_GLOBAL_EXPOSURE_USDC`：本机器人新增加的全局风险上限，100 USDC 资金建议不超过 80。
- `COUNT_EXISTING_POSITIONS_IN_GLOBAL_LIMIT=false`：默认不把你账户已有老仓位算进本机器人全局上限；如果想按账户总持仓硬控，改成 `true`。
- `INVENTORY_SKEW_THRESHOLD_USDC`：某 token 持仓超过该值后降低该 token 买单价格。
- `STOP_LOSS_PCT`：单边仓位亏损超过该比例时停止加仓，并尝试挂卖单退出。
- `TAKE_PROFIT_PCT`：单边仓位盈利超过该比例时优先挂卖单落袋。
- `MAX_MARKET_LOSS_USDC`：当前市场未实现亏损超过该美元数时进入 close-only。
- `MAX_MIDPOINT_MOVE_BPS`：midpoint 单轮跳变过大时暂停新买单，避免新闻/赛况冲击。
- `CLOSE_ONLY_HOURS_BEFORE_END`：临近结算前只减仓不开新仓；需要表格或 market info 有结束时间。
- `ORDER_TYPE=FOK`：主方案。订单必须立即全量成交，否则交易所直接取消整单，不留下挂单。
- `POST_ONLY=false`：FOK 是 taker 执行风格，不能和 post-only 同时使用。
- `FOK_PRICE_BUFFER_BPS`：FOK 激进限价 buffer。BUY 会在 `max(current_ask, predicted_mid)` 上加 buffer；SELL 会在 `min(current_bid, predicted_mid)` 上减 buffer。
- `FOK_MIN_EDGE_BPS`：触发 FOK 的最小预测优势。BUY 要求 `predicted_mid >= ask * (1 + edge)`；SELL 要求 `predicted_mid <= bid * (1 - edge)`。
- `CANCEL_UNFILLED_AFTER_MS`：FOK 模式下默认 0，因为不会留下挂单。
- `PREDICTION_LATENCY_MS`：你的预测窗口。建议用 `latency_probe.py` 测出来的 p95 延迟填写。
- `PREDICTION_EDGE_BPS`：默认 OBI 预测器的影响系数，盘口不平衡为 1 时预测 midpoint 偏移多少 bps。
- `MIN_PREDICTION_CONFIDENCE`：默认 OBI 预测器的最低置信度。低于阈值时不偏移预测价，回到当前 midpoint。
- `ORDERBOOK_IMBALANCE_LEVELS`：计算 OBI 使用盘口前几档深度。

## 延迟测量

先运行安全模式，不会发单：

```bash
uv run python latency_probe.py --samples 20
```

它会分别统计：

- `quote_fetch_ms`：从发起 orderbook 请求到收到盘口快照。
- `local_sign_ms`：本地构造并签名订单的时间，不提交到 CLOB。
- `safe_horizon_ms`：`quote_fetch_ms + local_sign_ms`，这是 dry-run/本地路径可测的预测窗口。

如果你要测真实提交路径，需要显式打开实盘探针：

```bash
DRY_RUN=false uv run python latency_probe.py --samples 5 --post-probe
```

实盘探针会用当前市场最小订单量发一笔 FOK BUY。它会额外输出：

- `post_ack_ms`：提交订单到 CLOB 返回响应的时间。
- `cancel_ack_ms`：只有非 FOK fallback 需要撤单时才有意义；FOK 正常会成交或自动取消。
- `live_horizon_ms`：`quote_fetch_ms + local_sign_ms + post_ack_ms`，更接近“看到行情后订单到达订单簿”的预测窗口。

保守做法是把 p95 的 `live_horizon_ms` 填到 `PREDICTION_LATENCY_MS`。如果只跑安全模式，就先用 p95 的 `safe_horizon_ms`。

## 短期预测扩展

`predictors.py` 里有统一接口：

```python
class ShortHorizonPredictor:
    def predict(self, features: MarketFeatures) -> Prediction:
        ...
```

你后续可以在这里叠加更多因子，例如短窗口 mid return、盘口撤单率、trade flow、YES/NO 联动价差、外部比赛/新闻信号，或者直接加载模型。只要返回 `Prediction(predicted_mid, edge_bps, confidence, reason)`，`MarketMaker` 就会用预测 midpoint 报价。

当前默认实现是 `ImbalancePredictor`：用前 N 档 bid/ask 深度计算 OBI：

```text
OBI = (bid_depth - ask_depth) / (bid_depth + ask_depth)
predicted_mid = mid * (1 + OBI * PREDICTION_EDGE_BPS / 10000)
```

如果置信度低于 `MIN_PREDICTION_CONFIDENCE`，预测价不偏移。

## 启动

Dry run：

```bash
uv run python main.py
```

确认报价合理、撤单范围正确后再实盘：

```bash
DRY_RUN=false uv run python main.py
```

日志会同时输出到 console 和 `logs/market_maker.log`。

如果实盘下单报 `invalid signature`，先运行：

```bash
uv run python check_auth.py
```

签名类型参考：

- `SIGNATURE_TYPE=0`：EOA / MetaMask 直连钱包，`FUNDER_ADDRESS` 通常等于私钥地址。
- `SIGNATURE_TYPE=1`：Polymarket proxy wallet，常见于 email/Google/Magic 登录的老流程。
- `SIGNATURE_TYPE=2`：Gnosis Safe 老流程。
- `SIGNATURE_TYPE=3`：deposit wallet / ERC-1271，新 API 用户推荐流程。

`invalid signature` 通常不是 market id 问题，而是 `PK`、`SIGNATURE_TYPE`、`FUNDER_ADDRESS` 三者不匹配。

## 市场选择建议

小资金优先选：

- 有 Liquidity Rewards 的市场。
- 高成交量、盘口连续、有稳定双边深度的市场。
- 体育/高频关注市场通常更适合试跑，但要避开临近结算、伤停信息剧烈变化、赔率快速跳动的时段。

生产环境建议显式配置 `TOKEN_ID_YES` 和 `TOKEN_ID_NO`。`AUTO_SELECT_REWARD_MARKET=true` 只是辅助发现 rewards 市场，不建议无人工确认就实盘。

## VPS 部署

伦敦 `eu-west-2` VPS 上建议用 `systemd`：

```ini
[Unit]
Description=Poly Maker Lightweight Bot
After=network-online.target

[Service]
WorkingDirectory=/opt/poly-maker
EnvironmentFile=/opt/poly-maker/.env
ExecStart=/usr/local/bin/uv run python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## 原仓库脚本

原 Google Sheets 版本仍可参考：

```bash
uv run python update_markets.py
uv run python update_stats.py
```

旧版 `trading.py` 依赖原 `poly_data` 体系和旧 `py-clob-client`。新轻量版使用 `py-clob-client-v2`，两者暂时并存，便于逐步迁移。

## 后续扩展

可以继续加：

- WebSocket orderbook，减少 REST 轮询延迟。
- reward scoring 更精细的订单宽度/大小计算。
- GTD 自动过期订单，降低断线残留风险。
- ML signal，对 `mid` 做轻微偏移，而不是只按中性 spread 报价。
- 更完整的 realized PnL 与成交归因。
