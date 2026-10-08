#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
研究 GUA/BTR/MAV 泵前是否有建仓痕迹。
拉泵前 ~7 天(4h 粒度)的 OI/价格区间/费率/多空比,逐棒算 z,看是否出现
"OI 堆 + 价横 + 费率背离"的慢建仓形态(即便扫描器没报,也看数据本身)。
纯研究,不调参。
"""
import os, sys, json, subprocess, statistics, datetime as dt

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import accumulation_scanner as A   # 复用 analyze 的 z 计算逻辑

SYMS = ["GUAUSDT", "BTRUSDT", "MAVUSDT"]
WIN = 42  # 7天 4h 基线


def study(sym):
    print(f"\n{'='*64}\n  {sym.replace('USDT','')} 泵前形态研究\n{'='*64}")
    st = int((dt.datetime.utcnow() - dt.timedelta(days=10)).timestamp() * 1000)
    en = int(dt.datetime.utcnow().timestamp() * 1000)

    k = A.get_json(f"{A.BIN}/fapi/v1/klines",
                   {"symbol": sym, "interval": "4h", "limit": A.KLINE_LIMIT})
    if not k:
        print("  K线拉取失败"); return
    kt = [dt.datetime.utcfromtimestamp(x[0]/1000) for x in k]
    op = [float(x[1]) for x in k]; cl = [float(x[4]) for x in k]
    amp = [abs(c/o-1) for o, c in zip(op, cl)]

    oi = A.get_json(f"{A.BIN}/futures/data/openInterestHist",
                    {"symbol": sym, "period": "1h", "limit": A.OI_LIMIT})
    oi_t = [dt.datetime.utcfromtimestamp(x["timestamp"]/1000) for x in oi] if oi else []
    oi_v = [float(x["sumOpenInterestValue"]) for x in oi] if oi else []

    fr = A.get_json(f"{A.BIN}/fapi/v1/fundingRate", {"symbol": sym, "limit": 1000})
    fr_t = [dt.datetime.utcfromtimestamp(x["fundingTime"]/1000) for x in fr] if fr else []
    fr_v = [float(x["fundingRate"]) for x in fr] if fr else []

    ls = A.get_json(f"{A.BIN}/futures/data/globalLongShortAccountRatio",
                    {"symbol": sym, "period": "4h", "limit": 500})
    ls_t = [dt.datetime.utcfromtimestamp(x["timestamp"]/1000) for x in ls] if ls else []
    ls_v = [float(x["longAccount"]) for x in ls] if ls else []

    oi_h, fr_h, ls_h = [], [], []
    oi_last = fr_last = ls_last = None
    rows = []
    for i, t in enumerate(kt):
        oi_last = A.at_ffill(oi_t, oi_v, t, oi_last)
        fr_last = A.at_ffill(fr_t, fr_v, t, fr_last)
        ls_last = A.at_ffill(ls_t, ls_v, t, ls_last)
        if oi_last is not None: oi_h.append(oi_last)
        if fr_last is not None: fr_h.append(fr_last)
        if ls_last is not None: ls_h.append(ls_last)
        oi_z = A.z_last(oi_h, WIN)
        rg_z = A.z_last(amp[:i+1], WIN)
        fr_z = A.z_last(fr_h, WIN)
        ls_z = A.z_last(ls_h, WIN)
        # 持续 OI 计数
        persist = 0
        for z in reversed([A.z_last(oi_h[:j+1], WIN) for j in range(len(oi_h))]):
            if z >= 2.0: persist += 1
            else: break
        # 签名
        oi_high = oi_z >= 2.0 and persist >= 6
        flat = rg_z <= -1.0
        S1 = oi_high and flat
        S2 = oi_high and fr_z <= 0
        S3 = ls_z <= -1.5 and flat
        signs = "".join(c for c, v in [("1",S1),("2",S2),("3",S3)] if v)
        rows.append((t, oi_z, rg_z, fr_z, ls_z, persist, signs))

    # 打印最近 7 天(约 42 棒) + 标记泵前窗口(最后 6 棒≈24h)
    print(f"  {'时间(UTC)':16} {'OI_z':>6} {'区间_z':>7} {'费率_z':>7} {'净多_z':>7} {'OI持续':>6}  签名")
    print("  " + "-"*58)
    for r in rows[-42:]:
        tag = " <==泵前" if r[0] >= dt.datetime.utcnow()-dt.timedelta(days=1) else ""
        print(f"  {r[0].strftime('%m-%d %H:%M'):16} {r[1]:+6.2f} {r[2]:+7.2f} {r[3]:+7.2f} {r[4]:+7.2f} {r[5]:>6}  {r[6] or '-'}{tag}")

    # 结论: 泵前 24h 内是否出现 >=2 签名
    pre = [r for r in rows if r[0] >= dt.datetime.utcnow()-dt.timedelta(days=1)]
    hits = [r for r in pre if len(r[6]) >= 2]
    print(f"\n  泵前 24h 内出现 >=2 签名: {'是' if hits else '否'}")
    if hits:
        for h in hits:
            print(f"    {h[0].strftime('%m-%d %H:%M')} 签名 {h[6]} | OIz={h[1]:.2f} 区间z={h[2]:.2f} 费率z={h[3]:.2f} 净多z={h[4]:.2f}")
    else:
        # 看泵前 OI 是否整体偏高(哪怕没到签名)
        oi_pre = [r[1] for r in pre]
        print(f"  泵前 OI_z 均值={sum(oi_pre)/len(oi_pre):.2f} (若>0 说明有堆仓但未达2σ/持续)")


for s in SYMS:
    study(s)
