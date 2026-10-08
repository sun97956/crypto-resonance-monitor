#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Crypto Accumulation Scanner

每 4h 扫描 universe.json 中的 Binance USDT 永续合约。

核心指标：
    1. Open Interest (OI)
    2. 4h Price Range
    3. Funding Rate
    4. Long / Short Ratio

计算每个指标相对自身历史基线的 z-score，
并通过 S1 / S2 / S3 accumulation signatures
筛选值得关注的结构性信号。

系统定位：
    - 不预测价格
    - 不自动交易
    - 不动态调参
    - 只发现值得进一步研究的信号

运行：
    python accumulation_scanner.py
"""

import os
import sys
import json
import time
import datetime
import statistics
import subprocess
import threading

from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


# ============================================================
# Paths
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

UNIVERSE_PATH = os.path.join(
    BASE_DIR,
    "universe.json",
)

ALERT_LOG_PATH = os.path.join(
    BASE_DIR,
    "accumulation_alerts.log",
)

STATE_PATH = os.path.join(
    BASE_DIR,
    "alert_state.json",
)

BINANCE_FAPI = "https://fapi.binance.com"


# ============================================================
# Frozen strategy parameters
# ============================================================

OI_Z_THRESHOLD = 2.0
RANGE_Z_FLAT = -1.0
FUNDING_Z_MAX = 0.0
LONG_SHORT_Z_MAX = -1.5

OI_PERSISTENCE_WINDOWS = 6
MIN_SIGNATURES = 2
COOLDOWN_HOURS = 24

# 42 个 4h 基线窗口 + 额外窗口用于 persistence
BASELINE_4H_WINDOWS = 42
KLINE_LIMIT = 60

# OI 为 1h 数据，只需要覆盖最近约 9 天即可完成
# 42 个 4h baseline + 6 个 persistence windows。
OI_LIMIT = 220

# Funding 默认每 8h 一次，200 条已经远超所需窗口。
FUNDING_LIMIT = 200

# Long/Short 使用 4h 数据，100 条已经足够。
LONG_SHORT_LIMIT = 100


# ============================================================
# Performance
# ============================================================

MAX_WORKERS = 12

REQUEST_TIMEOUT = (5, 15)
REQUEST_RETRIES = 2

_thread_local = threading.local()


def get_session():
    """为每个线程复用一个 requests Session。"""

    if not hasattr(_thread_local, "session"):
        session = requests.Session()

        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64)"
            )
        })

        _thread_local.session = session

    return _thread_local.session


# ============================================================
# HTTP
# ============================================================

def curl_json(url, params=None):
    """
    requests 无法访问 Binance 时使用 curl fallback。
    """

    try:
        from urllib.parse import urlencode

        full_url = url

        if params:
            full_url = f"{url}?{urlencode(params)}"

        result = subprocess.run(
            [
                "curl",
                "-s",
                "--max-time",
                "15",
                "-A",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                full_url,
            ],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=20,
        )

        if not result.stdout:
            return None

        return json.loads(result.stdout)

    except Exception:
        return None


def get_json(url, params=None):
    """
    高效请求 Binance API。

    4xx 中的 403 / 418 / 451 直接 fallback curl，
    避免无意义等待。

    429 / 5xx 才进行短暂 retry。
    """

    session = get_session()

    for attempt in range(REQUEST_RETRIES):

        try:
            response = session.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

            if response.ok:
                return response.json()

            status = response.status_code

            # Binance 常见网络访问限制：
            # 不再重复等待，直接尝试 curl。
            if status in (403, 418, 451):
                break

            # Rate limit / server error
            if status == 429 or status >= 500:
                if attempt < REQUEST_RETRIES - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue

            return None

        except Exception:

            if attempt < REQUEST_RETRIES - 1:
                time.sleep(1.0 * (attempt + 1))

    return curl_json(url, params)


# ============================================================
# Statistics
# ============================================================

def z_last(values, baseline_window):
    """
    最后一个值相对之前 baseline_window 个值的 z-score。
    """

    if len(values) < baseline_window + 2:
        return 0.0

    baseline = values[-(baseline_window + 1):-1]

    mean = statistics.fmean(baseline)
    std = statistics.pstdev(baseline)

    if std < 1e-12:
        return 0.0

    return (values[-1] - mean) / std


def forward_fill(times, values, target_time, last_value):
    """
    使用 <= target_time 的最近值。
    """

    best = last_value

    for timestamp, value in zip(times, values):

        if timestamp <= target_time:
            best = value
        else:
            break

    return best


# ============================================================
# Local state
# ============================================================

def load_universe():
    if not os.path.exists(UNIVERSE_PATH):
        raise FileNotFoundError(
            "找不到 universe.json"
        )

    with open(
        UNIVERSE_PATH,
        "r",
        encoding="utf-8",
    ) as f:

        universe = json.load(f)

    if not isinstance(universe, list):
        raise ValueError(
            "universe.json 必须是 symbol 列表"
        )

    return universe


def load_alert_state():

    if not os.path.exists(STATE_PATH):
        return {}

    try:

        with open(
            STATE_PATH,
            "r",
            encoding="utf-8",
        ) as f:

            state = json.load(f)

        return (
            state
            if isinstance(state, dict)
            else {}
        )

    except Exception:
        return {}


def save_alert_state(state):

    with open(
        STATE_PATH,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2,
        )


# ============================================================
# Analyze one symbol
# ============================================================

def analyze_symbol(symbol):
    """
    分析单个交易对。

    Returns:
        symbol, signs, detail
    """

    # --------------------------------------------------------
    # 1. 4h Kline
    # --------------------------------------------------------

    klines = get_json(
        f"{BINANCE_FAPI}/fapi/v1/klines",
        {
            "symbol": symbol,
            "interval": "4h",
            "limit": KLINE_LIMIT,
        },
    )

    if not klines:
        return symbol, set(), {}

    candle_times = [
        datetime.datetime.utcfromtimestamp(
            item[0] / 1000
        )
        for item in klines
    ]

    opens = [
        float(item[1])
        for item in klines
    ]

    closes = [
        float(item[4])
        for item in klines
    ]

    price_ranges = [
        abs(close / open_price - 1)
        for open_price, close
        in zip(opens, closes)
        if open_price != 0
    ]

    # --------------------------------------------------------
    # 2. Open Interest
    # --------------------------------------------------------

    oi_data = get_json(
        f"{BINANCE_FAPI}/futures/data/openInterestHist",
        {
            "symbol": symbol,
            "period": "1h",
            "limit": OI_LIMIT,
        },
    )

    oi_times = [
        datetime.datetime.utcfromtimestamp(
            item["timestamp"] / 1000
        )
        for item in oi_data
    ] if oi_data else []

    oi_values = [
        float(item["sumOpenInterestValue"])
        for item in oi_data
    ] if oi_data else []

    # --------------------------------------------------------
    # 3. Funding Rate
    # --------------------------------------------------------

    funding_data = get_json(
        f"{BINANCE_FAPI}/fapi/v1/fundingRate",
        {
            "symbol": symbol,
            "limit": FUNDING_LIMIT,
        },
    )

    funding_times = [
        datetime.datetime.utcfromtimestamp(
            item["fundingTime"] / 1000
        )
        for item in funding_data
    ] if funding_data else []

    funding_values = [
        float(item["fundingRate"])
        for item in funding_data
    ] if funding_data else []

    # --------------------------------------------------------
    # 4. Long / Short Ratio
    # --------------------------------------------------------

    long_short_data = get_json(
        f"{BINANCE_FAPI}/futures/data/globalLongShortAccountRatio",
        {
            "symbol": symbol,
            "period": "4h",
            "limit": LONG_SHORT_LIMIT,
        },
    )

    long_short_times = [
        datetime.datetime.utcfromtimestamp(
            item["timestamp"] / 1000
        )
        for item in long_short_data
    ] if long_short_data else []

    long_short_values = [
        float(item["longAccount"])
        for item in long_short_data
    ] if long_short_data else []

    # --------------------------------------------------------
    # Align all series to 4h candles
    # --------------------------------------------------------

    oi_history = []
    funding_history = []
    long_short_history = []

    oi_last = None
    funding_last = None
    long_short_last = None

    oi_z_series = []
    range_z_series = []
    funding_z_series = []
    long_short_z_series = []

    for index, timestamp in enumerate(candle_times):

        oi_last = forward_fill(
            oi_times,
            oi_values,
            timestamp,
            oi_last,
        )

        funding_last = forward_fill(
            funding_times,
            funding_values,
            timestamp,
            funding_last,
        )

        long_short_last = forward_fill(
            long_short_times,
            long_short_values,
            timestamp,
            long_short_last,
        )

        if oi_last is not None:
            oi_history.append(oi_last)

        if funding_last is not None:
            funding_history.append(
                funding_last
            )

        if long_short_last is not None:
            long_short_history.append(
                long_short_last
            )

        oi_z_series.append(
            z_last(
                oi_history,
                BASELINE_4H_WINDOWS,
            )
        )

        range_z_series.append(
            z_last(
                price_ranges[:index + 1],
                BASELINE_4H_WINDOWS,
            )
        )

        funding_z_series.append(
            z_last(
                funding_history,
                BASELINE_4H_WINDOWS,
            )
        )

        long_short_z_series.append(
            z_last(
                long_short_history,
                BASELINE_4H_WINDOWS,
            )
        )

    if not oi_z_series:
        return symbol, set(), {}

    # --------------------------------------------------------
    # Current z-scores
    # --------------------------------------------------------

    oi_z = oi_z_series[-1]
    range_z = range_z_series[-1]
    funding_z = funding_z_series[-1]
    long_short_z = long_short_z_series[-1]

    # --------------------------------------------------------
    # OI persistence
    # --------------------------------------------------------

    oi_persistence = 0

    for z in reversed(oi_z_series):

        if z >= OI_Z_THRESHOLD:
            oi_persistence += 1
        else:
            break

    oi_high = (
        oi_z >= OI_Z_THRESHOLD
        and oi_persistence
        >= OI_PERSISTENCE_WINDOWS
    )

    price_flat = (
        range_z <= RANGE_Z_FLAT
    )

    # --------------------------------------------------------
    # Signatures
    # --------------------------------------------------------

    s1 = (
        oi_high
        and price_flat
    )

    s2 = (
        oi_high
        and funding_z <= FUNDING_Z_MAX
    )

    s3 = (
        long_short_z <= LONG_SHORT_Z_MAX
        and price_flat
    )

    signs = set()

    if s1:
        signs.add("S1")

    if s2:
        signs.add("S2")

    if s3:
        signs.add("S3")

    detail = {
        "oi_z": round(oi_z, 2),
        "oi_persist": oi_persistence,
        "range_z": round(range_z, 2),
        "funding_z": round(funding_z, 2),
        "long_short_z": round(
            long_short_z,
            2,
        ),
        "last_price": round(
            closes[-1],
            6,
        ),
    }

    return symbol, signs, detail


# ============================================================
# Logging
# ============================================================

def write_alerts(timestamp, alerts):

    if not alerts:
        return

    with open(
        ALERT_LOG_PATH,
        "a",
        encoding="utf-8",
    ) as f:

        f.write(
            f"\n===== "
            f"{timestamp:%Y-%m-%d %H:%M}"
            f" | 命中 {len(alerts)} 币 =====\n"
        )

        for symbol, signs, detail in alerts:

            name = symbol.replace(
                "USDT",
                "",
            )

            sign_text = "".join(
                sorted(signs)
            )

            f.write(
                f"[{name}] "
                f"签名={sign_text} | "
                f"OI_z={detail['oi_z']} "
                f"(持续{detail['oi_persist']}窗) "
                f"Range_z={detail['range_z']} "
                f"Funding_z={detail['funding_z']} "
                f"LS_z={detail['long_short_z']} "
                f"Price={detail['last_price']}\n"
            )


# ============================================================
# Main
# ============================================================

def main():

    universe = load_universe()
    state = load_alert_state()

    now = datetime.datetime.now()
    now_ts = time.time()

    total = len(universe)

    print(
        f"[{now:%Y-%m-%d %H:%M}] "
        f"扫描 {total} 个标的..."
    )

    alerts = []

    completed = 0

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                analyze_symbol,
                symbol,
            ): symbol
            for symbol in universe
        }

        for future in as_completed(futures):

            symbol = futures[future]
            completed += 1

            try:

                symbol, signs, detail = (
                    future.result()
                )

            except Exception as exc:

                print(
                    f"  ERR {symbol}: {exc}"
                )

                continue

            print(
                f"\r进度 "
                f"{completed}/{total}",
                end="",
                flush=True,
            )

            # 至少命中两个 signatures
            if len(signs) < MIN_SIGNATURES:
                continue

            last_alert_ts = (
                state.get(symbol, {})
                .get("last_ts", 0)
            )

            # 24h cooldown
            if (
                now_ts - last_alert_ts
                < COOLDOWN_HOURS * 3600
            ):
                continue

            alerts.append(
                (
                    symbol,
                    signs,
                    detail,
                )
            )

            state[symbol] = {
                "last_ts": now_ts
            }

    print()

    write_alerts(
        now,
        alerts,
    )

    save_alert_state(
        state
    )

    print(
        f"完成。"
        f"扫描 {total} 个标的，"
        f"命中 {len(alerts)} 个。"
    )


if __name__ == "__main__":
    main()