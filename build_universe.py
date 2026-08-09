#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
构建建仓扫描器标的池：币安中低市值永续（非前100、有 USDT 永续、正常交易）。
输出 universe.json。仅 2 次公开请求，轻量。
"""
import os
import sys
import json
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BIN = "https://fapi.binance.com"
BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "universe.json")
# base 名含这些的跳过（稳定币 / 法币对 / 杠杆代币）
EXCLUDE_HINT = ("UP", "DOWN", "BULL", "BEAR", "USD", "USDC", "TUSD", "DAI", "FDUSD", "EUR", "BTC", "ETH")


def get_json(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=20)
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"GET_ERR {url}: {e}")
    return None


def main():
    info = get_json(f"{BIN}/fapi/v1/exchangeInfo")
    if not info:
        print("exchangeInfo 拉取失败"); return
    syms = [s["symbol"] for s in info.get("symbols", [])
            if s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"]

    tick = get_json(f"{BIN}/fapi/v1/ticker/24hr")
    if not tick:
        print("ticker 拉取失败"); return
    vol = {t["symbol"]: float(t.get("quoteVolume", 0)) for t in tick}

    ranked = sorted([s for s in syms if s in vol],
                    key=lambda s: vol[s], reverse=True)
    # 排除前 100（大盘），只留中低市值层
    mid = ranked[100:]

    keep = []
    for s in mid:
        base = s[:-4]
        if any(h in base for h in EXCLUDE_HINT):
            continue
        keep.append(s)
        if len(keep) >= 300:   # 中部 300 个（砍前100大盘后取300）
            break

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(keep, f, ensure_ascii=False, indent=0)
    print(f"已写入 {len(keep)} 个中低市值永续 -> universe.json")
    print("示例:", keep[:10])


if __name__ == "__main__":
    main()
