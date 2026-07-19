#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Crypto Resonance Monitor — 实时异动共振监控 + Telegram 推送。

零业务依赖（仅 requests + 标准库）：直接打 Binance 公开 REST，
不依赖 ccxt / pyarrow / Freqtrade；不发单，只检测 + 推送。

信号逻辑（样本外验证有效，参数冻结）：
  - 4 维度 z-score：波动z / 量z（价格面）+ 费率z / OI变化率z（结构面）
  - 共振 = 至少一个价格面异常 且 至少一个结构面异常
  - 阈值 |z| >= Z_TH（默认 5），约 99.999% 分位外 → 极稀有、平时安静
  - 验证：40 币/55 天，信号后 6h 波动 2.3x、24h 1.6x 基线（波动放大）

配置：同目录 config.json（见 config.example.json）。Token 从 .env 或环境变量读取。
运行：python monitor.py
"""
import os
import sys
import time
import json
import datetime
import statistics

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ENV_PATH = os.path.join(BASE_DIR, ".env")
STATE_PATH = os.path.join(BASE_DIR, "crypto_alert_state.json")
LOG_PATH = os.path.join(BASE_DIR, "crypto_alert.log")
BIN = "https://fapi.binance.com"

# 默认参数（config.json 可覆盖）
DEFAULTS = {
    "z_threshold": 5.0,
    "cooldown_hours": 6,
    "vol_win": 288, "qvol_win": 288, "fr_win": 168, "oi_win": 168,
    "timeout": 20,
    "telegram_chat_id": "",
    "universe": [],
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except Exception:
        pass
    return cfg


CFG = load_config()
Z_TH = float(CFG["z_threshold"])
VOL_WIN = int(CFG["vol_win"])
QVOL_WIN = int(CFG["qvol_win"])
FR_WIN = int(CFG["fr_win"])
OI_WIN = int(CFG["oi_win"])
COOLDOWN_H = int(CFG["cooldown_hours"])
TIMEOUT = int(CFG["timeout"])
CHAT_ID = str(CFG.get("telegram_chat_id") or "")
UNIVERSE = CFG.get("universe") or []


# ---------------- 工具 ----------------
def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.datetime.now()}] {msg}\n")
    except Exception:
        pass


def load_env_value(key):
    """优先从同目录 .env 读，回退环境变量。"""
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line[len(key) + 1:].strip().strip('"').strip("'")
    except Exception:
        pass
    return os.environ.get(key)


def send_telegram(text, retries=2):
    token = load_env_value("TELEGRAM_BOT_TOKEN")
    if not token:
        log("NO_TELEGRAM_TOKEN")
        return False
    if not CHAT_ID:
        log("NO_CHAT_ID")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        chunks.append(cur)
    ok_all = True
    for ch in chunks:
        for attempt in range(retries + 1):
            try:
                r = requests.post(url, json={"chat_id": CHAT_ID, "text": ch,
                                             "disable_web_page_preview": True}, timeout=30)
                if r.ok:
                    break
                ok_all = False
                log(f"TG_SEND_FAIL {r.status_code} {r.text[:160]}")
            except Exception as e:
                ok_all = False
                log(f"TG_SEND_ERR attempt{attempt} {e}")
                if attempt < retries:
                    time.sleep(3)
    return ok_all


def get_json(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        if r.ok:
            return r.json()
    except Exception as e:
        log(f"GET_ERR {url} {e}")
    return None


def zscore_series(vals, win):
    """返回最后一个点的 z-score（用其之前 win 窗口的均值/标准差）。"""
    n = len(vals)
    if n < win + 2:
        return 0.0, 0.0
    window = vals[-(win + 1):-1]
    mu = statistics.fmean(window)
    sd = statistics.pstdev(window)
    if sd < 1e-12:
        return 0.0, 0.0
    last = vals[-1]
    return (last - mu) / sd, last


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(st):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
    except Exception:
        pass


# ---------------- 单币检测 ----------------
def detect(symbol):
    """返回 (共振bool, 明细dict)。"""
    k = get_json(f"{BIN}/fapi/v1/klines", {"symbol": symbol, "interval": "5m", "limit": VOL_WIN + 30})
    if not k:
        return False, {}
    opens = [float(x[1]) for x in k]
    closes = [float(x[4]) for x in k]
    volumes = [float(x[5]) for x in k]
    bodies = [abs(c - o) / o for o, c in zip(opens, closes)]
    qz, _ = zscore_series(volumes, QVOL_WIN)
    vz, _ = zscore_series(bodies, VOL_WIN)
    last_dir = 1 if closes[-1] >= opens[-1] else -1

    fr = get_json(f"{BIN}/fapi/v1/fundingRate", {"symbol": symbol, "limit": 500})
    fr_z = 0.0
    fr_last = 0.0
    if fr:
        fr_vals = [float(x["fundingRate"]) for x in fr]
        fr_last = fr_vals[-1]
        fr_z, _ = zscore_series(fr_vals, FR_WIN)

    oi = get_json(f"{BIN}/futures/data/openInterestHist",
                  {"symbol": symbol, "period": "1h", "limit": OI_WIN + 5})
    oi_z = 0.0
    oi_chg = 0.0
    if oi:
        oi_vals = [float(x["sumOpenInterestValue"]) for x in oi]
        oi_chg = (oi_vals[-1] - oi_vals[-2]) / oi_vals[-2] if len(oi_vals) >= 2 and oi_vals[-2] else 0.0
        oir = [((oi_vals[i] - oi_vals[i - 1]) / oi_vals[i - 1]) if oi_vals[i - 1] else 0.0
               for i in range(1, len(oi_vals))]
        if len(oir) >= 2:
            oi_z, _ = zscore_series(oir, OI_WIN)

    body_last = bodies[-1] * 100

    price_anom = (abs(vz) >= Z_TH) or ((abs(qz) >= Z_TH) and last_dir != 0)
    struct_anom = (abs(fr_z) >= Z_TH) or (abs(oi_z) >= Z_TH)
    resonance = price_anom and struct_anom

    trig = []
    if abs(vz) >= Z_TH:
        trig.append(("波动", f"单根波动 {body_last:.2f}%，是近期均值的 {abs(vz):.1f} 倍标准差"))
    if abs(qz) >= Z_TH and last_dir != 0:
        trig.append(("量", f"成交量异常放大（{abs(qz):.1f}σ）"))
    if abs(fr_z) >= Z_TH:
        side = "做多" if fr_last > 0 else "做空"
        trig.append(("费率", f"资金费率 {fr_last*10000:.1f}‱，相对自身历史 {abs(fr_z):.1f}σ 异常（{side}拥挤）"))
    if abs(oi_z) >= Z_TH:
        trig.append(("OI", f"持仓量 1h 变化 {oi_chg*100:+.1f}%，{abs(oi_z):.1f}σ 异常（杠杆骤增）"))

    detail = {
        "vol_z": round(vz, 2), "qvol_z": round(qz, 2),
        "fr_z": round(fr_z, 2), "oi_z": round(oi_z, 2),
        "dir": "+" if last_dir > 0 else "-",
        "trig": trig,
    }
    return resonance, detail


# ---------------- 主流程 ----------------
def main():
    if not UNIVERSE:
        log("NO_UNIVERSE (config.json 缺失或为空)")
        return
    log("START scan")
    state = load_state()
    now = time.time()
    hits = []

    for sym in UNIVERSE:
        try:
            res, det = detect(sym)
        except Exception as e:
            log(f"DET_ERR {sym} {e}")
            continue
        if not res:
            continue
        last = state.get(sym, {}).get("last_ts", 0)
        if now - last < COOLDOWN_H * 3600:
            continue
        hits.append((sym, det))
        state.setdefault(sym, {})["last_ts"] = now

    save_state(state)

    if not hits:
        log("NO HITS (silent)")
        return

    lines = [f"🔥 异动共振告警 | {datetime.datetime.now().strftime('%m-%d %H:%M')} | 命中 {len(hits)} 币"]
    lines.append("=" * 28)
    for sym, det in hits:
        name = sym.replace("USDT", "")
        d = det["dir"]
        lines.append(f"\n【{name}】{'↑看涨' if d == '+' else '↓看跌'} 方向异动")
        for dim, txt in det.get("trig", []):
            lines.append(f"  · {dim}：{txt}")
        lines.append(f"  含义：价格与杠杆/费率同时极端异常，预计接下来几小时波动明显放大。")
    lines.append("=" * 28)
    lines.append("提示：这是『波动放大』预警，不是涨跌预测，也不构成交易建议。收到后可自行决定是否关注。")
    msg = "\n".join(lines)
    ok = send_telegram(msg)
    log(f"HITS={len(hits)} SENT_OK={ok}")
    print(msg)


if __name__ == "__main__":
    main()
