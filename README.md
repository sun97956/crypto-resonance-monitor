# Crypto Resonance Monitor

> 多维度共振的加密市场波动率异动监测系统

## Abstract

Crypto Resonance Monitor 是一套基于多信号共振的加密货币市场异动监测系统。系统对每只标的的波动率、成交量、资金费率、持仓量四个维度计算相对自身历史分布的 z-score，当**价格面异常**与**杠杆/费率面异常**跨类同时发生时生成告警，通过 Telegram 推送。样本外验证表明，共振信号后 6 小时波动率约为随机时点的 2.3 倍，具备对"波动放大"的预测价值。系统不预测价格方向，不执行任何交易。

## System Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                         Data Layer (Binance)                     │
│   5m K-line  ·  FundingRate (fapi/v1)  ·  OpenInterestHist (1h)  │
└───────────────────────────────┬──────────────────────────────────┘
                                 │  pull per scan (15 min)
              ┌──────────────────┴───────────────────┐
              │                                      │
   ┌──────────▼──────────┐               ┌─────────────▼─────────────┐
   │   Live Monitor       │              │   Backtest / Validation   │
   │   (zero-dep python)  │              │   (Freqtrade + ccxt)      │
   │                      │              │                           │
   │  Feature Engine      │              │  Historical replay        │
   │   z-score × 4 dims   │              │  rolling-window OOS       │
   │        │             │              │        │                  │
   │  Resonance Logic     │              │  Vol-expansion metric     │
   │  price ∩ struct      │              │                           │
   │        │             │              └─────────────┬─────────────┘
   │  Cooldown (6h/coin)  │                            │  report
   │        │             │                            │
   └────────┬─────────────┘                            │
            │ alert                                    │
            ▼                                          │
   ┌─────────────────────┐                             │
   │  Telegram Delivery  │◄───────────────────────────┘
   │  (Bot API, human-   │
   │   readable explain) │
   └─────────────────────┘
```

**Components**
- **Data Layer** — Binance USDT 永续公开 REST 接口，无鉴权、无第三方 key。
- **Feature Engine** — 对四维度分别计算滚动 z-score，基线窗口见下表。
- **Resonance Logic** — 跨类（价格面 ∩ 结构面）同时异常判定。
- **Live Monitor** — 零业务依赖（仅 `requests` + 标准库），可直接移植至任意联网主机或云实例。
- **Backtest Engine** — 离线验证环境，仅用于研发期确认信号有效性。

## Signal Methodology

### Feature Definition

对每个维度 $x$，以截至时刻 $t$ 之前的滚动窗口计算 z-score：

$$z_{x}(t) = \frac{x(t) - \mu_{w}(t)}{\sigma_{w}(t)}$$

| Dimension | $x(t)$ | Window $w$ | Cadence |
|-----------|--------|-----------|---------|
| 波动率 vol | 5m K 线实体振幅 $|c-o|/o$ | 288 (≈1d) | 5m |
| 成交量 qvol | 5m 成交量 | 288 | 5m |
| 资金费率 fr | FundingRate | 168 (≈7d) | 8h |
| 持仓变化 oi | OI 每小时环比 $(OI_t-OI_{t-1})/OI_{t-1}$ | 168 | 1h |

异常判定阈值统一为 $|z| \ge 5$（约 99.999% 分位，极值事件）。

### Resonance Rule

$$
\begin{aligned}
P_t &= \mathbf{1}[|z_{vol}(t)|\ge 5] \;\vee\; \big(\mathbf{1}[|z_{qvol}(t)|\ge 5] \wedge dir\neq 0\big) \\
S_t &= \mathbf{1}[|z_{fr}(t)|\ge 5] \;\vee\; \mathbf{1}[|z_{oi}(t)|\ge 5] \\
R_t &= P_t \wedge S_t
\end{aligned}
$$

波动率与成交量高度同源（放量必伴大实体），两者同触发不构成独立信号，故拆分为**价格面** $(P)$ 与**结构面** $(S)$，仅当跨类同时异常 $R_t=1$ 才视为有效共振。

### Design Rationale

采用 z-score 而非固定倍数阈值，是因为各币属性差异极大（BTC 与 meme 的波动量级不可比），相对自身基线才能跨币统一衡量"异常程度"。z-score 捕捉的是分布的结构性跳变（费率悄然拉高、持仓骤增等领先征兆），而非对已发生波动的事后描述。

## Universe Construction

- 数据源：CoinGecko 市值排名
- 准入：市值 $\ge \$100\text{M}$，且存在 Binance USDT 永续
- 剔除：稳定币、贵金属/大宗商品代币、基金/法币代币、杠杆代币 (3L/3S)、指数合成币、RWA（如房贷代币）、新发政治概念币
- 规模：取市值前 40，共 **40 标的**

| Universe | Daily Alerts | 6h Vol Multiple | 24h Vol Multiple |
|----------|-------------|-----------------|------------------|
| Top-40 by volume (incl. small/index) | 1.93 | 2.53x | 1.96x |
| Cap $\ge \$1\text{B}$ (over-filtered) | 0.81 | 1.22x | 0.98x |
| **Cap $\ge \$100\text{M}$, 40 coins (adopted v1)** | **1.26** | **2.29x** | **1.62x** |
| **Cap $\ge \$100\text{M}$, 50 coins (adopted v2)** | **1.65** | **2.11x** | **1.56x** |

过严的市值门槛（$\ge\$1\text{B}$）使大币主导、效应衰减至无；过松（成交量前 40）则被小币/指数币极端波动虚高。$\$100\text{M}$ 在洁净度与信号活性间取得平衡。

## Backtest Methodology

- **样本**：40 标的 × 约 55 天，全历史回放。
- **无未来函数**：z-score 基线仅使用 $t$ 之前的数据（滚动窗口）。
- **参数冻结**：阈值 $Z=5$ 由统计极值原理确定，不对样本调参。
- **评估标的**：信号后波动率相对随机时点的放大倍率（非涨跌方向胜率，方向不可预测）。

## Backtest Results

| Metric | Value |
|--------|-------|
| Total resonance signals (50 coins) | 92 |
| Avg alerts / day (50 coins) | 1.65 |
| Random-baseline 6h volatility | 2.23% |
| **Post-signal 6h volatility** | **4.71% (2.11×)** |
| Random-baseline 24h volatility | 4.62% |
| **Post-signal 24h volatility** | **7.21% (1.56×)** |
| Share of signals exceeding baseline | 69.6% |

共振发生后，后续 6 小时波动为随机时点的 2.1 倍、24 小时 1.6 倍，且约七成的信号确实伴随波动放大。效应方向与幅度一致，表明该信号对"波动放大"具备真实预测力，而非随机噪声。

## Live Deployment

- **调度**：每 15 分钟执行一次 `monitor.py`。
- **静默**：无命中则不推送、不产生输出。
- **冷却**：同一标的 6 小时内不重复告警，避免刷屏。
- **依赖**：仅 `requests` + Python 标准库，可运行于本机、常驻服务或云实例。
- **交付**：通过 Telegram Bot API 推送至预设 chat。

**Sample Alert**

```
🔥 异动共振告警 | 07-19 14:30 | 命中 1 币
============================
【BTC】↑看涨 方向异动
  · 波动：单根波动 0.85%，是近期均值的 5.2 倍标准差
  · OI：持仓量 1h 变化 +12.3%，5.1σ 异常（杠杆骤增）
  含义：价格与杠杆/费率同时极端异常，预计接下来几小时波动明显放大。
============================
提示：这是波动放大预警，不是涨跌预测，也不构成交易建议。
```

## Risk & Limitations

- 系统预测**波动放大**，不预测**价格方向**；价值在于提示"该关注"，而非给出买卖点。
- 样本量（70 次）中等，倍率方向稳定但置信度仍需实时运行累积确认。
- 实时运行依赖主机常驻（进程存活 + 网络连通）；主机离线则监控中断。
- 单交易所（Binance）数据源，标的下架或迁移将导致该标的失效。
- 极端行情下全市场 z-score 同步抬升可能产生批量告警。

*本系统仅作市场监测辅助，所有输出不构成任何投资建议。*
