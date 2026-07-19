"""重建干净币池 universe.json（中等标准）：
1. CoinGecko 市值 >= $1B
2. 排除：稳定币 / 黄金 / 基金 / 法币 / 杠杆代币
3. 硬编码排除：FIGR_HELOC(房贷RWA) / CC(Canton) / WLFI(特郎普新币) / USDGO(稳定币)
4. 必须有 Binance USDT 永续（否则无 OI/费率历史可拉）
5. 按市值取前 40
输出 universe.json（Binance 裸 symbol，如 BTCUSDT），并打印数据覆盖情况。
"""
import json
import re
import ccxt
import os

allc = json.load(open("cg_markets_raw.json"))

# ---- CG 过滤 ----
STABLE = re.compile(r"^(USDT|USDC|DAI|USDS|USDE|USD1|USDG|USYC|USDY|PYUSD|RLUSD|USDD|USDF|BFUSD|USDK|FDUSD|TUSD|BUSD|GUSD|USDP|USDGO)$", re.I)
GOLD = re.compile(r"^(PAXG|XAUT|DGX|TBTC|BUIDL)$", re.I)
NAME_BAD = re.compile(r"(stable|usd |usd$|dollar|gold|tether|fund|yield|reserve|fiat|heloc|mortgage)", re.I)
LEV = re.compile(r"(3L|3S|5L|5S|UP|DOWN|BULL|BEAR)", re.I)
HARD_EXCLUDE = {"FIGR_HELOC", "CC", "WLFI", "USDGO"}  # RWA房贷/新政治币/稳定币，真该剔

def is_bad(c):
    s = (c.get("symbol") or "").upper()
    n = c.get("name") or ""
    if s in HARD_EXCLUDE: return True
    if STABLE.search(s): return True
    if GOLD.search(s): return True
    if LEV.search(s): return True
    if NAME_BAD.search(n): return True
    return False

mc_pool = [c for c in allc if (c.get("market_cap") or 0) >= 1e8 and not is_bad(c)]
mc_pool.sort(key=lambda c: c["market_cap"], reverse=True)
print(f"CG 市值>=1亿 且过滤后: {len(mc_pool)} 个")

# ---- 必须有 Binance 永续 ----
EX = ccxt.binance({"options": {"defaultType": "swap"}})
EX.load_markets()
perp = {m["base"].upper(): s for s, m in EX.markets.items()
        if m.get("quote") == "USDT" and (m.get("type") == "swap" or m.get("contract") is True)}

clean = []
for c in mc_pool:
    base = (c.get("symbol") or "").upper()
    if base in perp:
        # perp[base] 形如 BTC/USDT:USDT 可能带到期后缀，取干净裸 symbol
        sym = perp[base].split("-")[0]          # BTC/USDT:USDT
        raw = sym.replace("/", "").split(":")[0]  # BTCUSDT
        clean.append((raw, c["name"], c["market_cap"]))
    if len(clean) >= 40:
        break
print(f"有 Binance 永续且入池: {len(clean)} 个")

# 保存为裸 symbol
uni = [s for s, _, _ in clean]
json.dump(uni, open("universe.json", "w"))

# 数据覆盖检查
KDIR = "user_data/data/binance/futures"
HIST = "user_data/history"
def base_of(sym): return f"{sym[:-4]}_USDT_USDT"
print("\n数据覆盖（K=K线 FR=费率 OI=持仓量）：")
for s in uni:
    b = base_of(s)
    k = os.path.exists(f"{KDIR}/{b}-5m-futures.feather")
    fr = os.path.exists(f"{HIST}/{b}_fr.feather")
    oi = os.path.exists(f"{HIST}/{b}_oi.feather")
    flag = "OK" if (k and fr and oi) else ("缺" + ("K" if not k else "") + ("F" if not fr else "") + ("O" if not oi else ""))
    print(f"  {s:10s} {flag}")
print("\nDONE clean_universe")
