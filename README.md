# Poly-Maker

Poly-Maker 现在包含两套思路：

- 原仓库逻辑：复杂版 Polymarket 做市/统计工具，依赖 Google Sheets、WebSocket、仓位合并脚本。
- 新增轻量版：适合约 100 USDC 起步的小资金 Polymarket Market Making Bot，使用官方 `py-clob-client-v2`，通过 `.env` 配置，默认 `DRY_RUN=true`。

> 风险提醒：这是真金白银交易代码。做市可能因为盘口跳变、单边成交、结算事件、API/网络异常、奖励规则变化而亏损。先 dry run，再用极小订单实盘验证。

## 现有代码逻辑

原项目主要文件：

- `main.py`：现在是轻量版机器人入口。
- `config.py`：从 `.env` 读取交易、风控、市场选择参数。
- `polymarket_adapter.py`：封装 `py-clob-client-v2`，处理 token 解析、orderbook、持仓、撤单、GTC limit order。
- `market_maker.py`：核心做市循环，定期计算 midpoint 附近买单，执行 exposure 风控和 inventory skew。
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
5. 在 YES/NO token 上按 `mid * (1 - spread)` 挂 maker BUY GTC 订单。
6. 如果单 token 或单市场 exposure 超限，停止增加风险。
7. 如果某边 inventory 偏大，就把该边 bid 再压低，减少继续买入概率。
8. 每轮记录 market exposure、global exposure、未实现 PnL 和奖励估算。

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
```

参数说明：

- `DRY_RUN=true`：只打印拟挂单，不真的发单。第一次必须先 dry run。
- `ORDER_NOTIONAL_USDC`：每笔订单用美元金额控制，例如 5 USDC。
- Polymarket 有按 shares 计的最小订单量，bot 会读取 `min_order_size`；如果你的美元订单太小导致 shares 不够，会跳过该边报价。
- `PRICE_SPREAD_BPS`：相对 midpoint 的单边距离，120 表示 1.2%。
- `MAX_MARKET_EXPOSURE_USDC`：单市场最大风险，建议 20-30。
- `MAX_GLOBAL_EXPOSURE_USDC`：本机器人新增加的全局风险上限，100 USDC 资金建议不超过 80。
- `COUNT_EXISTING_POSITIONS_IN_GLOBAL_LIMIT=false`：默认不把你账户已有老仓位算进本机器人全局上限；如果想按账户总持仓硬控，改成 `true`。
- `INVENTORY_SKEW_THRESHOLD_USDC`：某 token 持仓超过该值后降低该 token 买单价格。
- `POST_ONLY=true`：尽量保证 maker，不主动吃单。

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
