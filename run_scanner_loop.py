#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
调度器：每隔 SCAN_INTERVAL 小时跑一次 accumulation_scanner.py。
用法：python run_scanner_loop.py  （后台运行；或交给系统 cron / 任务计划程序）
本文件只做「定时 + 调用」，业务全在 accumulation_scanner.py。
"""
import os
import sys
import time
import subprocess

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
SCANNER = os.path.join(BASE, "accumulation_scanner.py")
SCAN_INTERVAL = 4 * 3600  # 4 小时


def main():
    while True:
        print(f"[{time.strftime('%Y-%m-%d %H:%M')}] 触发扫描...")
        try:
            subprocess.run([sys.executable, SCANNER], cwd=BASE, check=False)
        except Exception as e:
            print(f"扫描异常: {e}")
        print(f"下次扫描 {SCAN_INTERVAL//3600} 小时后。\n")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
