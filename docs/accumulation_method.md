# Accumulation Scanner — Methodology

> **版本：v0**
>
> 本文档定义当前 Accumulation Scanner 的信号假设、指标和触发规则。
>
> 当前参数作为初始冻结假设使用，**尚未经过正式样本外验证**。
>
> 本系统用于市场筛选，不预测价格方向，不自动交易。

---

## 1. 系统定位

Accumulation Scanner 的目标是从 Binance USDT 永续市场中筛选出：

> **持仓结构出现变化，但价格仍相对平静的标的。**

核心观察逻辑是：

```text
OI 上升
    +
价格没有同步明显扩张
    +
Funding / Long-Short positioning 没有显示明显多头拥挤
    ↓
值得进一步研究
```

系统不是判断“这个币一定会上涨”，而是帮助人工从大量标的中找到值得进一步查看的候选。

---

## 2. 为什么观察这四个维度

当前系统使用四个市场维度：

| 维度 | 含义 | 数据 |
|---|---|---|
| Open Interest | 杠杆仓位规模变化 | Binance Open Interest |
| 4h Price Range | 价格是否保持平静 | Binance Klines |
| Funding Rate | 永续合约多空拥挤程度 | Binance Funding Rate |
| Long / Short Ratio | 账户多空配置变化 | Binance Global Long/Short Ratio |

其中最核心的是：

```text
Open Interest
+
Price Range
```

OI 增加代表市场中的合约持仓规模正在增加。

如果与此同时价格没有出现明显扩张，则形成一种值得关注的“结构变化与价格变化不一致”。

Funding Rate 和 Long/Short Ratio 用于进一步判断这种结构是否伴随着明显的多头拥挤或空头倾向。

---

## 3. 相对自身历史进行标准化

不同代币的价格波动和持仓规模差异很大。

因此系统不使用类似：

```text
OI 增加 10%
价格波动 2%
```

这样的绝对阈值进行跨币比较。

而是计算每个指标相对于该标的近期历史分布的 z-score：

```text
z(t) = [x(t) - mean(baseline)] / std(baseline)
```

其中：

- `x(t)` = 当前指标
- `mean(baseline)` = 历史基线均值
- `std(baseline)` = 历史基线标准差

这样得到的是：

> **当前状态距离该标的自身正常水平有多远。**

---

## 4. 时间粒度与基线

当前系统每 **4 小时**进行一次扫描。

主要指标使用约 **7 天**的历史窗口建立近期基线。

| 指标 | 当前实现 | 主要数据源 |
|---|---|---|
| OI | 1h 数据对齐到 4h K 线 | `/futures/data/openInterestHist` |
| Price Range | 4h K 线实体振幅 | `/fapi/v1/klines` |
| Funding | 历史 Funding Rate | `/fapi/v1/fundingRate` |
| Long / Short | 4h Global Long/Short Account Ratio | `/futures/data/globalLongShortAccountRatio` |

当前代码的主要历史窗口为：

```text
BASELINE_4H_WINDOWS = 42
```

即：

```text
42 × 4h ≈ 7 days
```

---

## 5. Accumulation Signatures

系统目前定义三个主要签名：

```text
S1
S2
S3
```

最终要求至少两个签名同时成立。

---

### S1 — OI Build-up + Quiet Price

规则：

```text
OI z-score >= 2.0

AND

OI 条件连续 >= 6 个 4h 窗口

AND

Price Range z-score <= -1.0
```

即：

```text
OI 显著高于自身近期常态
+
这种状态持续约 24 小时
+
价格波动处于较低水平
```

这是当前系统最核心的 accumulation signature。

其基本假设是：

> 如果仓位结构持续增加，但价格仍然保持相对平静，这种背离值得进一步研究。

需要注意：

**OI 上升本身并不等于“有人在建仓”。**

它也可能来自：

- 杠杆交易增加
- 对冲
- 短期投机
- 多空双方同时增加仓位

因此 S1 不能单独作为最终信号。

---

### S2 — OI Build-up + Funding Divergence

规则：

```text
OI z-score >= 2.0

AND

OI 条件连续 >= 6 个 4h 窗口

AND

Funding z-score <= 0
```

含义是：

```text
OI 显著增加
+
Funding 没有明显高于自身正常水平
```

基本假设：

> 如果 OI 明显增加，但 funding 没有同步出现高位正值，则这种仓位增加并不表现为明显的多头拥挤。

因此它可以作为 S1 的补充确认。

---

### S3 — Low Long/Short Ratio + Quiet Price

规则：

```text
Long/Short z-score <= -1.5

AND

Price Range z-score <= -1.0
```

含义：

```text
账户多头占比明显低于自身近期常态
+
价格仍然比较平静
```

这里的基本假设是：

> 如果市场定位逐渐偏向空头，但价格仍没有明显下跌，则这种 positioning / price divergence 值得关注。

但同样需要注意：

**低 Long/Short Ratio 并不自动意味着未来一定发生轧空。**

---

## 6. 为什么要求 OI 持续 6 个窗口

单个 4h OI 异常可能只是短期噪声。

因此系统要求：

```text
OI z >= 2.0
```

连续出现：

```text
6 个 4h 窗口
```

即约：

```text
24 小时
```

设计思路是：

```text
一次性尖峰
    ↓
可能只是噪声

持续一天以上
    ↓
更像一种市场状态
```

这里的“持续”是为了降低 false positive，而不是证明存在真实的“主力建仓”。

---

## 7. 最终触发条件

三个 signature 分别独立判断：

```text
S1
S2
S3
```

最终：

```text
满足数量 >= 2
        ↓
    Candidate
```

即：

```text
S1 + S2
S1 + S3
S2 + S3
S1 + S2 + S3
```

都会进入告警。

而：

```text
只有 S1
只有 S2
只有 S3
```

不会触发最终告警。

这样做的目的，是避免依赖单一指标。

---

## 8. Alert Cooldown

同一标的存在：

```text
24 小时 cooldown
```

即同一个标的在 24 小时内不会因为相同结构反复写入新的告警。

原因是 accumulation-like structure 更接近一种持续状态，而不是瞬时事件。

---

## 9. Universe Construction

当前监控池包含：

```text
300 Binance USDT perpetual contracts
```

Universe 由 `build_universe.py` 生成。

流程：

```text
Binance USDT Perpetuals
        ↓
筛选正在交易的合约
        ↓
按 24h quote volume 排名
        ↓
排除最高成交量 Top 100
        ↓
取之后的 300 个合约
        ↓
universe.json
```

因此：

> 当前 universe 基于 Binance **24 小时成交额排名**构建。

这样做的目的，是避开交易量最高的一批主流合约，寻找相对不那么拥挤的交易标的。

---

## 10. 当前参数

| Parameter | Value |
|---|---:|
| Scan interval | 4h |
| Baseline | 42 × 4h |
| OI threshold | z >= 2.0 |
| OI persistence | 6 × 4h |
| Quiet price | Range z <= -1.0 |
| Funding | z <= 0 |
| Long/Short | z <= -1.5 |
| Minimum signatures | 2 / 3 |
| Cooldown | 24h |

这些参数目前属于：

> **rule-based initial assumptions**

并不是根据某一个代币历史数据反复调参得到的最优参数。

---

## 11. 数据处理

扫描器对不同时间粒度的数据进行对齐。

基本流程：

```text
Binance REST API
       ↓
Historical observations
       ↓
Align to 4h candle timestamps
       ↓
Build rolling history
       ↓
Calculate z-scores
       ↓
Evaluate S1 / S2 / S3
       ↓
Generate candidate
```

OI 使用 1h 数据，因此在计算 4h 状态时需要将较高频的数据映射到对应的 4h 时间点。

---

## 12. What the signal means

一个最终命中的标的只能说明：

> **当前市场结构满足至少两个 accumulation signatures。**

它不能说明：

```text
一定上涨
一定暴涨
一定存在主力
一定发生轧空
```

正确的使用方式应该是：

```text
Scanner
   ↓
Candidate
   ↓
Human research
   ↓
Look at chart / news / liquidity / tokenomics / catalysts
   ↓
Independent judgment
```

因此，该系统更接近：

> **Market Structure Screening Tool**

而不是：

> **Trading Signal Generator**

---

## 13. Validation Plan

当前规则尚未被证明具有稳定的预测能力。

后续验证应该回答三个问题：

### 13.1 Signal 是否领先于价格变化

对于某次明显的价格扩张事件：

```text
event
  ↑
  |
candidate signal?
```

需要判断 accumulation signal 是否在事件之前出现，而不是事件发生以后才出现。

---

### 13.2 Signal 是否比随机窗口更集中

可以比较：

```text
P(signal | pre-event window)
```

与：

```text
P(signal | random window)
```

如果前者明显更高，则说明 signal 可能具有一定领先性。

---

### 13.3 False Positive 是否过高

重点观察：

```text
命中数量
+
重复命中
+
最终没有发生明显变化的标的
```

如果系统长期产生大量候选，但绝大多数没有后续结构变化，则需要重新评估规则。

---

## 14. Validation Principle

验证过程中应该遵循：

```text
先定义规则
      ↓
冻结参数
      ↓
运行观察 / 样本外测试
      ↓
记录结果
      ↓
再决定是否修改规则
```

而不是：

```text
看到某个币成功
      ↓
修改参数
      ↓
让这个币重新命中
```

后者会产生严重的 selection bias / overfitting。

---

## 15. Limitations

### 1. Single-exchange data

当前主要数据来自 Binance。

因此，如果市场结构发生在其他交易所，系统可能无法观察到。

### 2. OI ≠ Accumulation

OI 上升只能说明持仓规模增加。

它无法单独区分：

```text
accumulation
vs
speculation
vs
hedging
vs
short build-up
```

### 3. Long/Short Ratio ≠ Future Direction

Long/Short Ratio 描述的是账户 positioning。

低 ratio 不意味着价格必然上涨。

### 4. Rules are not yet validated

当前阈值是研究假设，不应描述为已经证明有效。

### 5. API availability

扫描依赖 Binance 公开 REST API。

网络、地区访问限制或 API 异常都可能导致某些标的数据缺失。

---

## 16. Research Philosophy

这个项目当前最重要的不是增加更多指标，而是验证一个简单的问题：

> **市场结构中的“仓位变化—价格平静”背离，是否能够比随机状态更早地筛选出值得研究的标的？**

因此当前版本刻意保持规则简单：

```text
4 dimensions
        ↓
z-score
        ↓
3 signatures
        ↓
>= 2 signatures
        ↓
candidate
```

只有在这个基础版本经过充分观察和验证之后，才有必要进一步增加：

- 更多市场数据
- 跨交易所信息
- 事件 / 新闻信息
- 自动化研究解释
- Agent-based analysis

当前版本的核心目标是：

> **先建立一个简单、透明、可验证的 market-structure screening baseline。**

---

> This project is for research and educational purposes only. It does not constitute investment advice.