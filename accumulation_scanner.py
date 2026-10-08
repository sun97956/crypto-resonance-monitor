#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
建仓扫描器（Accumulation Scanner）— 实时 4h 扫描 + 双日志。

设计原则（红线，见 ACCUMULATION_METHOD.md）：
  - 阈值全部来自方法论文档、按统计原理冻结，运行时绝不调整。
  - 不做涨跌预测，只标记「值得关注」。
  - 仅币安公开 REST（免费）。CoinGecko Pro 无衍生品历史，此处不用。
  - OI/费率/多空比历史币安仅滚动保留 ~31 天 → 本系统只验证近 31 天的泵。

每 4h 跑一次：
  1) 对 universe.json 每个币算 4 维相对自身基线的 z-score
  2) 判定 S1/S2/S3 签名，≥2 命中 → 写 accumulation_alerts.log（带时间戳）
  3) 同时写 pumps_detected.log（24~48h 涨≥+20% 的币，供每周对账）
  4) 冷却 24h 同币不重复告警
  5) 可选推送 Telegram（config.json 配 token/chat_id）

运行：python accumulation_scanner.py
"""
import os
import sys
import json
import time
import datetime
import statistics

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import subprocess
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UNIV_PATH = os.path.join(BASE_DIR, "universe.json")
CFG_PATH = os.path.join(BASE_DIR, "config.json")
ALERT_LOG = os.path.join(BASE_DIR, "accumulation_alerts.log")
PUMP_LOG = os.path.join(BASE_DIR, "pumps_detected.log")
BIN = "https://fapi.binance.com"

# ===== 冻结参数（来自 ACCUMULATION_METHOD.md，原理驱动，不得运行时调整）=====
OI_Z_TH = 2.0          # 2σ 上尾
RANGE_Z_FLAT = -1.0    # 价格区间 1σ 下尾 = 异常平静
FR_Z_MAX = 0.0         # 费率不高于自身基线
LS_Z_MAX = -1.5        # 1.5σ 下尾 = 异常净空
PERSIST_WIN = 6        # OI 异常须连续 ≥6 窗口 ≈24h
MIN_SIG = 2            # S1/S2/S3 至少满足 2 个
COOLDOWN_H = 24
KLINE_LIMIT = 4 * 24 * 7 + 12   # 近 7 天 4h K线 + 余量 ≈ 84
OI_LIMIT = 31 * 24     # 31 天每小时 OI（接口约 744 上限）
WIN_4H = 42            # z 基线窗口 = 近 7 天（每 4h=42 根）
PUMP_PCT = 0.20        # 24~48h 涨≥20% 记为泵


def _curl_json(url, params=None):
    """curl 兜底：币安对 Python requests 偶发 451/SSL EOF，curl 实测更稳。"""
    try:
        import urllib.parse
        full = url
        if params:
            full = url + "?" + urllib.parse.urlencode(params)
        out = subprocess.run(
            ["curl", "-s", "-A",
             "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", full],
            capture_output=True, text=True, timeout=30)
        return json.loads(out.stdout)
    except Exception:
        return None


def get_json(url, params=None, retries=3, backoff=2.0):
    """优先 requests（带重试+退避），失败回退 curl。应对币安 451/SSL EOF。"""
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=25)
            if r.ok:
                return r.json()
            last = r.status_code
        except Exception as e:
            last = e
        if attempt < retries - 1:
            time.sleep(backoff * (attempt + 1))
    # requests 连续失败 → 回退 curl（仅一次，curl 本身稳）
    c = _curl_json(url, params)
    if c is not None:
        return c
    if isinstance(last, Exception):
        print(f"GET_ERR {url}: {last}")
    return None


def z_last(vals, win):
    """返回最后一点相对其之前 win 窗口的 z-score；数据不足/扁平返回 0.0。"""
    n = len(vals)
    if n < win + 2:
        return 0.0
    window = vals[-(win + 1):-1]
    mu = statistics.fmean(window)
    sd = statistics.pstdev(window)
    if sd < 1e-12:
        return 0.0
    return (vals[-1] - mu) / sd


def at_ffill(times, vals, t, last):
    """前向填充：取 ≤t 的最近值，缺失用 last。"""
    best = last
    for tt, vv in zip(times, vals):
        if tt <= t:
            best = vv
        else:
            break
    return best


def analyze(symbol):
    """返回 (signs:set, detail:dict)。无数据返回 (set(), {})。"""
    st = int((datetime.datetime.utcnow() - datetime.timedelta(days=32)).timestamp() * 1000)

    k = get_json(f"{BIN}/fapi/v1/klines",
                 {"symbol": symbol, "interval": "4h", "limit": KLINE_LIMIT})
    if not k:
        return set(), {}
    kt = [datetime.datetime.utcfromtimestamp(x[0] / 1000) for x in k]
    op = [float(x[1]) for x in k]
    cl = [float(x[4]) for x in k]
    amp = [abs(c / o - 1) for o, c in zip(op, cl)]   # 4h 实体振幅 = 价格区间

    oi = get_json(f"{BIN}/futures/data/openInterestHist",
                  {"symbol": symbol, "period": "1h", "limit": OI_LIMIT})
    oi_t = [datetime.datetime.utcfromtimestamp(x["timestamp"] / 1000) for x in oi] if oi else []
    oi_v = [float(x["sumOpenInterestValue"]) for x in oi] if oi else []

    fr = get_json(f"{BIN}/fapi/v1/fundingRate", {"symbol": symbol, "limit": 1000})
    fr_t = [datetime.datetime.utcfromtimestamp(x["fundingTime"] / 1000) for x in fr] if fr else []
    fr_v = [float(x["fundingRate"]) for x in fr] if fr else []

    ls = get_json(f"{BIN}/futures/data/globalLongShortAccountRatio",
                  {"symbol": symbol, "period": "4h", "limit": 500})
    ls_t = [datetime.datetime.utcfromtimestamp(x["timestamp"] / 1000) for x in ls] if ls else []
    ls_v = [float(x["longAccount"]) for x in ls] if ls else []

    # 逐 4h 棒累积（前向填充避免 None）
    oi_hist, fr_hist, ls_hist = [], [], []
    oi_last = fr_last = ls_last = None
    oi_z_series, flat_series, fr_z_series, ls_z_series = [], [], [], []
    for i, t in enumerate(kt):
        oi_last = at_ffill(oi_t, oi_v, t, oi_last)
        fr_last = at_ffill(fr_t, fr_v, t, fr_last)
        ls_last = at_ffill(ls_t, ls_v, t, ls_last)
        if oi_last is not None:
            oi_hist.append(oi_last)
        if fr_last is not None:
            fr_hist.append(fr_last)
        if ls_last is not None:
            ls_hist.append(ls_last)
        oi_z_series.append(z_last(oi_hist, WIN_4H))
        flat_series.append(z_last(amp[:i + 1], WIN_4H))
        fr_z_series.append(z_last(fr_hist, WIN_4H))
        ls_z_series.append(z_last(ls_hist, WIN_4H))

    if not oi_z_series or not flat_series:
        return set(), {}

    # 取最后一点 + 持续计数（OI 异常连续窗口数）
    oi_z = oi_z_series[-1]
    range_z = flat_series[-1]
    fr_z = fr_z_series[-1]
    ls_z = ls_z_series[-1]
    persist = 0
    for z in reversed(oi_z_series):
        if z >= OI_Z_TH:
            persist += 1
        else:
            break

    # 签名
    oi_high = (oi_z >= OI_Z_TH) and (persist >= PERSIST_WIN)
    flat = range_z <= RANGE_Z_FLAT
    S1 = oi_high and flat
    S2 = oi_high and (fr_z <= FR_Z_MAX)
    S3 = (ls_z <= LS_Z_MAX) and flat
    signs = set()
    if S1:
        signs.add("S1")
    if S2:
        signs.add("S2")
    if S3:
        signs.add("S3")

    detail = {
        "oi_z": round(oi_z, 2), "oi_persist": persist,
        "range_z": round(range_z, 2), "fr_z": round(fr_z, 2),
        "ls_z": round(ls_z, 2),
        "last_price": round(cl[-1], 6),
    }
    return signs, detail


def scan_pumps(symbol):
    """24~48h 涨幅 ≥20% 记为泵。返回 (bool, pct)。"""
    k = get_json(f"{BIN}/fapi/v1/klines",
                 {"symbol": symbol, "interval": "1d", "limit": 4})
    if not k or len(k) < 3:
        return False, 0.0
    closes = [float(x[4]) for x in k]
    # 24h 前 -> 现在；48h 前 -> 现在
    p24 = (closes[-1] / closes[-2] - 1) if len(closes) >= 2 and closes[-2] else 0
    p48 = (closes[-1] / closes[-3] - 1) if len(closes) >= 3 and closes[-3] else 0
    pct = max(p24, p48)
    return pct >= PUMP_PCT, round(pct * 100, 1)


def main():
    with open(UNIV_PATH, "r", encoding="utf-8") as f:
        universe = json.load(f)
    now = datetime.datetime.now()
    now_ts = time.time()
    print(f"[{now:%Y-%m-%d %H:%M}] 扫描 {len(universe)} 币...")

    # 冷却状态
    state = {}
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            state = json.load(f).get("_alert_state", {})
    except Exception:
        pass

    alerts = []
    pumps = []
    for sym in universe:
        try:
            signs, det = analyze(sym)
        except Exception as e:
            print(f"  ERR {sym}: {e}")
            continue
        # 泵检测（每个币每次都记）
        is_pump, pct = scan_pumps(sym)
        if is_pump:
            pumps.append((sym, pct))
        # 签名判定
        if len(signs) < MIN_SIG:
            continue
        last = state.get(sym, {}).get("last_ts", 0)
        if now_ts - last < COOLDOWN_H * 3600:
            continue
        alerts.append((sym, signs, det))
        state.setdefault(sym, {})["last_ts"] = now_ts

    # 写告警日志
    if alerts:
        with open(ALERT_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n===== {now:%Y-%m-%d %H:%M} | 命中 {len(alerts)} 币 =====\n")
            for sym, signs, det in alerts:
                name = sym.replace("USDT", "")
                f.write(f"[{name}] 签名 {''.join(sorted(signs))} | "
                        f"OIz={det['oi_z']}(持续{det['oi_persist']}窗) "
                        f"区间z={det['range_z']} 费率z={det['fr_z']} 净多z={det['ls_z']} "
                        f"价={det['last_price']}\n")
        # 回存冷却状态
        try:
            cfg = {}
            try:
                with open(CFG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                pass
            cfg["_alert_state"] = state
            with open(CFG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # 写泵日志
    if pumps:
        with open(PUMP_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n===== {now:%Y-%m-%d %H:%M} | 泵 {len(pumps)} 币 =====\n")
            for sym, pct in pumps:
                f.write(f"[{sym.replace('USDT','')}] +{pct}%\n")

    print(f"完成。告警 {len(alerts)} 币，泵 {len(pumps)} 币。")


if __name__ == "__main__":
    main()
