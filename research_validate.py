"""样本外验证：z-score 共振信号是否预示"未来波动率放大"（非涨跌方向）。
独立于 Freqtrade，读本地 feather。

防过拟合设计：
- 历史全部加载，z-score 滚动窗口只用到"该时刻之前"的数据（无未来函数）。
- 不调任何参数：Z_TH=3 固定，窗口固定。
- 衡量：信号后 6h/24h 实际波动 vs 全样本随机时点波动的均值倍率。
  若 ratio >> 1，说明信号真的预示异动（有用）；若 ≈1，则无信息量（可止损）。

波动定义：未来 N 根 5m K线 收盘价的"已实现波动率" = 收益率 std × sqrt(N)（年化风格）。
"""
import glob
import os
import json
import numpy as np
import pandas as pd

DATA = "user_data/data/binance/futures"
HIST = "user_data/history"
VOL_WIN, QVOL_WIN = 288, 288
FR_WIN, OI_WIN = 168, 168
Z_TH = 5.0


def load_universe():
    if os.path.exists("universe.json"):
        return json.load(open("universe.json"))
    return ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT",
            "BNB/USDT:USDT", "XRP/USDT:USDT", "ADA/USDT:USDT", "AVAX/USDT:USDT", "LINK/USDT:USDT"]


def sym_files(pair):
    # pair "BTC/USDT:USDT" -> "BTC_USDT_USDT"；也兼容裸 "BTCUSDT"
    if "/" in pair:
        return pair.replace("/", "_").replace(":USDT", "_USDT")
    return f"{pair[:-4]}_USDT_USDT"


def load_5m(pair):
    base = sym_files(pair)
    f = glob.glob(f"{DATA}/{base}-5m-futures.feather")
    if not f:
        return None
    d = pd.read_feather(f[0])
    d["date"] = d["date"].apply(lambda x: pd.Timestamp(x).tz_localize(None) if hasattr(x, "tz") and x.tz else pd.Timestamp(x))
    return d


def load_h(pair, kind):
    base = sym_files(pair)
    f = f"{HIST}/{base}_{kind}.feather"
    if not os.path.exists(f):
        return None
    d = pd.read_feather(f)
    d["date"] = pd.to_datetime(d["date"])
    col = {"oi": "oi", "fr": "fr"}[kind]
    return d.set_index("date")[col].sort_index()


def realized_vol(rets, n):
    """未来 n 根收益率序列的已实现波动率（std × sqrt(n)）"""
    if len(rets) < n:
        return np.nan
    return np.std(rets[:n]) * np.sqrt(n)


def main():
    raw = load_universe()
    # 裸 symbol "BTCUSDT" -> pair "BTC/USDT:USDT"
    pairs = []
    for s in raw:
        if "/" in s:
            pairs.append(s)
        else:
            pairs.append(f"{s[:-4]}/USDT:USDT")
    print(f"币池 {len(pairs)} 个")
    sig_vols_6, sig_vols_24 = [], []
    rand_vols_6, rand_vols_24 = [], []
    sig_count = 0

    for p in pairs:
        df = load_5m(p)
        if df is None:
            continue
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        n = len(df)
        rets = df["close"].pct_change().fillna(0).values

        # 价格维度 z
        body = (df["close"] - df["open"]).abs() / df["open"]
        vmu = body.rolling(VOL_WIN, min_periods=50).mean()
        vsd = body.rolling(VOL_WIN, min_periods=50).std()
        vol_z = ((body - vmu) / vsd).fillna(0).values
        qmu = df["volume"].rolling(QVOL_WIN, min_periods=50).mean()
        qsd = df["volume"].rolling(QVOL_WIN, min_periods=50).std()
        qvol_z = ((df["volume"] - qmu) / qsd).fillna(0).values
        qdir = np.sign(df["close"].values - df["open"].values)

        # 结构维度 z（本地历史，滚动用到时刻前）
        fr = load_h(p, "fr")
        oi = load_h(p, "oi")
        fr_z = np.zeros(n)
        if fr is not None and len(fr) > 20:
            frm = fr.rolling(FR_WIN, min_periods=20).mean()
            frs = fr.rolling(FR_WIN, min_periods=20).std()
            fz = ((fr - frm) / frs)
            fr_z = fz.reindex(df.index, method="ffill").fillna(0).values
        oi_z = np.zeros(n)
        if oi is not None and len(oi) > 20:
            oir = oi.pct_change(1)
            oim = oir.rolling(OI_WIN, min_periods=20).mean()
            ois = oir.rolling(OI_WIN, min_periods=20).std()
            oiz = ((oir - oim) / ois)
            oi_z = oiz.reindex(df.index, method="ffill").fillna(0).values

        price_anom = (np.abs(vol_z) >= Z_TH) | ((np.abs(qvol_z) >= Z_TH) & (qdir != 0))
        struct_anom = (np.abs(fr_z) >= Z_TH) | (np.abs(oi_z) >= Z_TH)
        signal = price_anom & struct_anom

        # 基线：全样本随机波动分布（用所有时点，不含未来泄漏——已用 shift）
        for i in range(VOL_WIN, n - 288):
            rv6 = realized_vol(rets[i+1:], 72)    # 6h
            rv24 = realized_vol(rets[i+1:], 288)  # 24h
            if np.isnan(rv6) or np.isnan(rv24):
                continue
            rand_vols_6.append(rv6)
            rand_vols_24.append(rv24)
            if signal[i]:
                sig_vols_6.append(rv6)
                sig_vols_24.append(rv24)
                sig_count += 1

    rand_6, rand_24 = np.mean(rand_vols_6), np.mean(rand_vols_24)
    sig_6 = np.mean(sig_vols_6) if sig_vols_6 else 0
    sig_24 = np.mean(sig_vols_24) if sig_vols_24 else 0
    days_total = len(rand_vols_6) / (288 * len(pairs)) if pairs else 0
    per_day = sig_count / days_total if days_total else 0
    print(f"币池 {len(pairs)} 个 | 观察约 {days_total:.0f} 天/币")
    print(f"信号命中数: {sig_count}  | 平均每日信号数: {per_day:.2f}")
    print(f"随机基线 6h波动={rand_6:.5f}  24h波动={rand_24:.5f}")
    print(f"信号后 6h波动={sig_6:.5f}  倍率={sig_6/rand_6:.2f}x" if sig_count else "无信号")
    print(f"信号后 24h波动={sig_24:.5f}  倍率={sig_24/rand_24:.2f}x" if sig_count else "")
    if sig_vols_6:
        above = sum(1 for x in sig_vols_6 if x > rand_6)
        print(f"信号后6h波动 > 随机基线占比: {above/len(sig_vols_6)*100:.1f}%")


if __name__ == "__main__":
    main()
