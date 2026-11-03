#!/usr/bin/env python3
"""回填「宽基 ETF 净流入」历史（本地直连数据库跑，不走 HTTP）。

为什么需要它：沪市份额接口只能按单个日期查，回填今年约 200 个交易日就是
200 次请求，整轮 5~8 分钟 —— 走 `/api/a-share/refresh` 会顶穿网关 60 秒。
所以历史回填在本地跑这个脚本（与 backfill_lhb.py 同一思路），
日常增量交给收盘轮（只重算最近 20 个交易日，约 30 秒）。

用法：
    python backfill_etf_flow.py                 # 默认回填今年
    python backfill_etf_flow.py 2025-06-01      # 指定起点
    python backfill_etf_flow.py 2026-01-01 --force
"""
from __future__ import annotations

import sys
import time

from app import cloud_db
from app.config import settings
from app.utils.etf_flow import NAME, SYMBOL, refresh_etf_flow
from app.utils.index_daily import ytd_start


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    since = args[0] if args else ytd_start()
    force = "--force" in sys.argv

    print(f"回填 {NAME}（{SYMBOL}）自 {since} 起，force={force}")
    if cloud_db.cloud_db_configured():
        print(f"数据库：云库（{settings.CLOUD_DB_ENDPOINT}）")
    else:
        print(f"数据库：本地 SQLite（{settings.DATABASE_URL}）")
    t0 = time.time()
    # 回填一律整体重算：区间内可能已有部分结果，重算能顺带修正口径
    result = refresh_etf_flow(since=since, force=True)
    cost = time.time() - t0

    print(f"耗时 {cost:.1f}s")
    print(f"窗口 {result['window']} 个交易日，算出 {result['fetched']} 天，"
          f"写入 {result['added']} 行，样本 {result['pool']} 只")
    if result.get("latest"):
        print(f"最新：{result['latest']['date']} {result['latest']['value']:+.2f} 亿元"
              f"（{result['latest']['codes']} 只）")
    if result.get("anomalies"):
        print(f"份额跳变（判为折算，已跳过）：{result['anomalies']}")
    if result.get("errors"):
        print(f"抓取失败 {len(result['errors'])} 处（多为当日份额尚未披露）：")
        for e in result["errors"][:8]:
            print("  ", e)
    if result.get("error"):
        print("整体失败：", result["error"])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
