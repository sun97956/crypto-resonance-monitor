# 部署指南

Crypto Resonance Monitor 是一个零依赖的 Python 脚本，可直接运行于本机、常驻服务或云实例。

## 依赖

- Python 3.8+
- 仅 `requests`：`pip install requests`

无需 ccxt / pyarrow / Freqtrade。脚本直接调用 Binance 公开 REST 与 Telegram Bot API。

## 1. 获取 Bot Token 与 Chat ID

1. 在 Telegram 找 [@BotFather](https://t.me/BotFather)，发送 `/newbot`，按提示创建 bot，拿到 **HTTP API Token**。
2. 与你的 bot 发一条任意消息（触发对话）。
3. 获取你的 **chat_id**：
   - 浏览器访问 `https://api.telegram.org/bot<TOKEN>/getUpdates`，在返回里找 `"chat":{"id":123456789}`。
   - 或把自己的 user_id 作为 chat_id（个人对话即用自己 id）。

## 2. 配置

复制模板并填入你的信息：

```bash
cp config.example.json config.json
```

编辑 `config.json`：

```json
{
  "telegram_chat_id": "你的chat_id",
  "z_threshold": 5.0,
  "cooldown_hours": 6,
  "universe": ["BTCUSDT", "ETHUSDT", "..."]
}
```

Token 不要写进 config.json（避免泄露）。在 `.env`（同目录）或环境变量里设置：

```bash
# .env
TELEGRAM_BOT_TOKEN=你的bot_token
```

或在 shell 中 `export TELEGRAM_BOT_TOKEN=你的bot_token`。

## 3. 运行一次（验证）

```bash
python monitor.py
```

无命中则静默退出（不推送）；有共振则 Telegram 收到告警。

## 4. 定时运行

### 方式 A：Linux / macOS — crontab

每 15 分钟执行一次：

```bash
crontab -e
# 添加：
*/15 * * * * cd /path/to/crypto-resonance-monitor && /usr/bin/python3 monitor.py >> run.log 2>&1
```

> 脚本无命中时不产生任何输出，不会刷日志。

### 方式 B：Windows — 任务计划程序

1. 打开"任务计划程序" → 创建基本任务。
2. 触发器：每天，重复间隔 15 分钟。
3. 操作：启动程序 `python`，参数 `monitor.py`，起始于项目目录。

### 方式 C：Hermes（桌面端）

若使用 Hermes，可将脚本挂为 `no_agent` cron 任务（脚本路径相对 `~/.hermes/scripts/`），每 15 分钟运行，无命中静默。

## 5. 自定义币池

`universe` 为 Binance USDT 永续 symbol 列表（如 `BTCUSDT`）。币池构建逻辑见 `research_build_universe.py`（按 CoinGecko 市值 ≥ $100M、剔除稳定币/指数/RWA/杠杆币，取前 40）。可直接在 `config.json` 增删标的。

## 6. 调参说明

- `z_threshold`：异常阈值，默认 5（极值分位，平时安静）。降低（如 4）会更灵敏但更频繁。
- `cooldown_hours`：同一标的两次告警的最小间隔，防刷屏。
- 其余窗口参数一般无需改动（已在样本外验证确定）。

> 阈值改动后建议重新跑 `research_validate.py` 确认信号频率与波动放大倍率仍合理，避免过拟合。
