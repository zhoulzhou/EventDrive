"""龙虎榜历史回填（一次性脚本，可重复执行）。

用法：
    # 回填最近 10 个交易日（本地 SQLite）
    python backfill_lhb.py --days 10

    # 回填到云库（线上用的那份数据）
    python backfill_lhb.py --cloud --days 10

    # 指定某一天（收盘后补抓当天）
    python backfill_lhb.py --cloud --date 2026-10-29

⚠ **必须用 --cloud 而不是环境变量**：config.py 里是 load_dotenv(override=True)，
  本地 .env 的 CLOUD_DB_ENABLED=false 会盖掉 shell 传进去的 true。

行为说明：
- 每个交易日**先删后写**（crud.upsert_lhb_day 的既定语义），重复执行不会堆重复行；
- 按「最近有数据的交易日」往前数，自动跳过周末与节假日（东财返回空即视为非交易日）；
- 位置（低位/中位/高位）需要逐只股票拉日线，回填十几天的历史时请求量会翻十倍，
  所以 --no-position 可关掉它（历史复盘用不到那么细的位置）；
- 失败不静默：结尾会打印每个交易日的落库条数与错误列表。

⚠ 云库模式下直接写线上数据，先确认要回填的天数。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import settings  # noqa: E402

# --cloud 必须在 import crud 之前生效：crud 在导入时就把 _USE_CLOUD 定死了
if "--cloud" in sys.argv:
    settings.CLOUD_DB_ENABLED = True

from app import crud  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.utils import lhb_refresh  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="龙虎榜历史回填")
    ap.add_argument("--days", type=int, default=5, help="回填最近 N 个交易日")
    ap.add_argument("--date", default=None, help="只抓指定交易日 YYYY-MM-DD")
    ap.add_argument("--cloud", action="store_true", help="写入云库（线上数据）")
    ap.add_argument("--no-position", action="store_true",
                    help="不计算个股位置（省掉逐只股票的日线请求）")
    args = ap.parse_args()

    print(f"数据源: {'云库' if crud.using_cloud_db() else '本地 SQLite'}")
    db = SessionLocal()
    try:
        if args.date:
            res = lhb_refresh.refresh_day(db, args.date,
                                          with_position=not args.no_position)
            days = [res]
        else:
            payload = lhb_refresh.refresh_recent(
                db, days=max(1, args.days),
                with_position=not args.no_position,
            )
            days = payload.get("days") or []
        total = 0
        for d in days:
            total += d["records"]
            print(f"  {d['trade_date']}: 榜单 {d['records']} 条 / 席位 {d['seats']} 条"
                  f"（清掉旧数据 {d['deleted']} 行）")
            if d.get("errors"):
                for msg in d["errors"][:5]:
                    print(f"    ! {msg}")
        print(f"完成：{len(days)} 个交易日，共 {total} 条榜单")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
