#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对账脚本（每周跑一次）：验证「建仓扫描器」灵不灵。

逻辑：
  - 读 pumps_detected.log：所有被记为「泵」的币 + 时间
  - 对每个泵币，回看它泵时间点之前的 4h 窗口（拉当时 OI/费率/价格）
  - 若泵前 7 天内扫描器曾告警过该币 → 算「提前命中」
  - 计算召回（泵中被提前标记的比例）与对照命中率，得出 Lift 雏形

注意：币安 OI 仅保留 ~31 天。若某泵距今天 >31 天，其泵前 OI 已不可得，
      该泵只能「定性」看（告警日志里有没有），无法用 OI 量化。
依赖：accumulation_alerts.log / pumps_detected.log（由 accumulation_scanner.py 产生）。
"""
import os
import re
import sys
import json
import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
ALERT_LOG = os.path.join(BASE, "accumulation_alerts.log")
PUMP_LOG = os.path.join(BASE, "pumps_detected.log")


def parse_pumps(path):
    """返回 [(date_str, [币名...])]。"""
    if not os.path.exists(path):
        return []
    out = []
    cur = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            m = re.match(r"=====\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*\|", line)
            if m:
                if cur:
                    out.append(cur)
                cur = (m.group(1), [])
                continue
            if cur and line.startswith("["):
                name = line[1:line.index("]")]
                cur[1].append(name)
    if cur:
        out.append(cur)
    return out


def parse_alerts(path):
    """返回 [(date_str, [币名...])]。"""
    if not os.path.exists(path):
        return []
    out = []
    cur = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            m = re.match(r"=====\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*\|", line)
            if m:
                if cur:
                    out.append(cur)
                cur = (m.group(1), [])
                continue
            if cur and line.startswith("["):
                name = line[1:line.index("]")]
                cur[1].append(name)
    if cur:
        out.append(cur)
    return out


def main():
    pumps = parse_pumps(PUMP_LOG)
    alerts = parse_alerts(ALERT_LOG)
    if not pumps:
        print("暂无数泵记录（扫描器跑几天后才有）。对账待数据积累。")
        return

    # 建告警索引：币名 -> [告警日期]
    from collections import defaultdict
    ad = defaultdict(list)
    for dt_s, names in alerts:
        for n in names:
            ad[n].append(dt_s)

    print(f"已记录泵事件 {len(pumps)} 次，告警会话 {len(alerts)} 次\n")
    pre_flagged = 0
    rows = []
    for pdt, names in pumps:
        pdate = datetime.datetime.strptime(pdt, "%Y-%m-%d %H:%M")
        for n in names:
            flagged = [d for d in ad.get(n, [])
                       if (pdate - datetime.datetime.strptime(d, "%Y-%m-%d %H:%M")).days
                       in range(0, 8)]   # 泵前 0~7 天
            hit = len(flagged) > 0
            pre_flagged += 1 if hit else 0
            rows.append((n, pdt, "提前命中" if hit else "未提前标记",
                         flagged[0] if hit else "-"))

    print(f"{'币':10} {'泵时间':16} {'结果':10} {'最早告警'}")
    print("-" * 50)
    for r in rows:
        print(f"{r[0]:10} {r[1]:16} {r[2]:10} {r[3]}")

    total = sum(len(n) for _, n in pumps)
    print("-" * 50)
    print(f"泵币总数={total}  其中泵前被标记={pre_flagged}")
    print(f"召回率(雏形) = {pre_flagged/total:.1%}")
    print("注：样本少时 Lift 不稳定；攒够 20+ 泵事件再下结论。")


if __name__ == "__main__":
    main()
