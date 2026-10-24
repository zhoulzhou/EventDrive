"""把存量数据从本地 SQLite 导入云数据库（PostgREST）。

一次性迁移脚本，幂等：可重复执行，已存在的记录会被 upsert 覆盖。

用法（需先配置云库三个环境变量）：
    set CLOUD_DB_ENABLED=true
    set CLOUD_DB_ENDPOINT=https://<app-domain>
    set CLOUD_DB_KEY=wbpk_xxx
    python migrate_snapshot_to_cloud.py

迁移范围（全部 11 张表）：
    快照类小表：macro_indicators / macro_indicator_history /
              stock_temp_indicators / stock_temp_history / crawl_logs
    大表    ：news / index_history / financial_reports / market_prices /
              market_strategy_state / company_valuations

⚠ 历史表按 (key, as_of) 定位，导入后云库里若已有同点会被「修正」而非追加，
  这是 upsert_history 的既定语义，符合预期。
"""
import os
import sqlite3
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import cloud_db as cdb  # noqa: E402
from app.config import settings  # noqa: E402

LOCAL_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "db.sqlite3")

# (云库表名, 本地表名, 需要导入的列, 唯一键)
#
# ⚠ created_at / updated_at / fetched_at 必须显式搬运：它们是页面
# 「数据更新于 …」与「数据已N 小时未落库」stale 判定的依据。
# 不带这两列的话云端会填server_default=now()，所有行都显示成「导入那一刻」，
# 数据年龄永远是 0，stale 提示形同虚设。
MIGRATIONS = [
    (
        "macro_indicators", "macro_indicators",
        ["key", "value", "as_of", "source", "detail",
         "created_at", "updated_at"], "key",
    ),
    (
        "macro_indicator_history", "macro_indicator_history",
        ["key", "value", "as_of", "source", "detail", "fetched_at"], None,
    ),
    (
        "stock_temp_indicators", "stock_temp_indicators",
        ["key", "value", "as_of", "source", "detail",
         "created_at", "updated_at"], "key",
    ),
    (
        "stock_temp_history", "stock_temp_history",
        ["key", "value", "as_of", "source", "detail", "fetched_at"], None,
    ),
    (
        "crawl_logs", "crawl_logs",
        ["task", "session", "source", "item_count", "status",
         "error_message", "duration", "crawl_time"], None,
    ),
]

# ---- 大表（第二步）----
#
# on_conflict 为该表的唯一键；None 表示无唯一键，走「查存在再写」。
# cols 需包含 created_at / updated_at（存量迁移依赖它们保持真实落库时间）。
BIG_MIGRATIONS = [
    (
        "news", "news",
        ["title", "content", "source", "publish_time", "url",
         "author", "summary", "image_path", "created_at", "updated_at"], "url",
    ),
    (
        "index_history", "index_history",
        ["date", "NASDAQCOM_2015", "VIXCLS_2015", "DGS2_2015", "DGS10_2015",
         "created_at", "updated_at"], "date",
    ),
    (
        "financial_reports", "financial_reports",
        ["stock_code", "stock_name", "report_date", "revenue", "operating_cost",
         "net_profit", "inventory", "accounts_receivable", "cash",
         "short_term_investment", "contract_liabilities", "shareholders_equity",
         "operating_cash_flow", "short_term_borrowing", "non_current_liab_due_1y",
         "long_term_borrowing", "bonds_payable", "interest_expense",
         "created_at", "updated_at"], "stock_code,report_date",
    ),
    (
        "market_prices", "market_prices",
        ["symbol", "name", "unit", "value", "date", "created_at", "updated_at"],
        "symbol,date",
    ),
    (
        "market_strategy_state", "market_strategy_state",
        ["symbol", "peak_value", "peak_date", "drawdown_date",
         "created_at", "updated_at"], "symbol",
    ),
    (
        "company_valuations", "company_valuations",
        ["company_name", "base_profit", "forecast_years", "growth_forecast",
         "transition_years", "growth_transition", "growth_perpetual", "discount_rate",
         "current_market_value", "enterprise_value", "created_at", "updated_at"], None,
    ),
]


def _serialize(col: str, value):
    """把 SQLite 取出的值转成 JSON 可传输 / PostgreSQL 可接受的形式。"""
    if value is None:
        return None
    if col == "crawl_time":
        return str(value)          # 'YYYY-MM-DD HH:MM:SS.ffffff'
    if isinstance(value, (int, float, str)):
        return value
    return str(value)


def read_local(table: str, columns: List[str]) -> List[Dict]:
    if not os.path.exists(LOCAL_DB):
        print(f"[skip] 本地库不存在: {LOCAL_DB}")
        return []
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(
            f"SELECT {', '.join(columns)} FROM {table}"
        )
        return [
            {c: _serialize(c, row[c]) for c in columns}
            for row in cur.fetchall()
        ]
    except sqlite3.Error as e:
        print(f"[error] 读取本地表 {table} 失败: {e}")
        return []
    finally:
        con.close()


def _normalize_utc_columns(rows: List[Dict], columns: List[str]) -> List[Dict]:
    """把本地库里被写成**本地时间**的时间列校正为 UTC。

    背景（实测结论，别再搞反）：
    - SQLite 的 ``CURRENT_TIMESTAMP``/``func.now()`` 返回 **UTC**（实测 09:15 == UTC 09:15）。
    - 但本项目多处用 ``datetime.now()``（**本地时间**）显式覆盖 updated_at，
      于是同一个库里两种基准混存：server_default 写的行是 UTC，
      被代码覆盖过的行是北京时间（本机 UTC+8）。
    - 页面按 UTC 基准算「数据已N 小时未落库」，若把本地时间当UTC，
      数据年龄会变成负数（如 -8.0），stale 判定随之失真。

    这里的处理：以「明显晚于当前 UTC」为启发式判据，把本地时间回退 8 小时。
    仅在偏差超过 1 小时且为正时生效，正常 UTC 值原样保留。
    """
    from datetime import datetime, timedelta, timezone

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    offset = (datetime.now() - datetime.now(timezone.utc).replace(tzinfo=None))
    # 本机相对 UTC 的偏移（北京时间 = +8h）
    shift = timedelta(hours=round(offset.total_seconds() / 3600))

    out = []
    fixed = 0
    for row in rows:
        item = dict(row)
        for col in columns:
            if col not in ("updated_at", "created_at", "fetched_at"):
                continue
            val = item.get(col)
            if not isinstance(val, str) or not val.strip():
                continue
            try:
                parsed = datetime.fromisoformat(val.strip().replace(" ", "T"))
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            # 未来时间（相对 UTC 明显超前）= 本地时间被当成了 UTC -> 回退偏移
            if parsed - now_utc > timedelta(hours=1):
                parsed = parsed - shift
                fixed += 1
            item[col] = parsed.strftime("%Y-%m-%d %H:%M:%S.%f")
        out.append(item)
    if fixed:
        print(f"      （已把 {fixed} 个本地时间戳校正为 UTC）")
    return out


def main() -> int:
    if not cdb.cloud_db_configured():
        print("未配置云数据库。请设置 CLOUD_DB_ENABLED / CLOUD_DB_ENDPOINT / CLOUD_DB_KEY")
        return 1

    print(f"云端: {settings.CLOUD_DB_ENDPOINT}")
    print(f"本地: {LOCAL_DB}\n")

    grand = 0
    for cloud_tbl, local_tbl, columns, on_conflict in MIGRATIONS:
        rows = read_local(local_tbl, columns)
        if not rows:
            print(f"[=] {local_tbl}: 本地无数据，跳过")
            continue

        # 时间列统一校正为 UTC（本地库存在 server_default 与 datetime.now() 混写的情况）
        rows = _normalize_utc_columns(rows, columns)

        before = cdb.count(cloud_tbl)

        if on_conflict:
            # 快照表：按唯一键 upsert（created_at/updated_at 交给云端默认）
            cdb.upsert(cloud_tbl, rows, on_conflict=on_conflict,
                       context=f"migrate:{local_tbl}")
            after = cdb.count(cloud_tbl)
            print(f"[+] {local_tbl}: 读入 {len(rows)} 行，云端 {before} -> {after}")
        else:
            # 历史表 / 日志表：无唯一键，需按内容去重后再写
            # crawl_logs 用 (task, session, crawl_time) 判重，
            # 历史表用 (key, as_of) —— 统一交给 upsert_history 处理
            if cloud_tbl == "crawl_logs":
                # 日志表没有 (key, as_of)，用 fetched_at 字段替代做判重键
                existing = cdb.select(
                    "crawl_logs", columns="task,session,crawl_time",
                )
                seen = {
                    (r["task"], r["session"], str(r["crawl_time"]))
                    for r in existing
                }
                todo = [
                    r for r in rows
                    if (r["task"], r["session"], str(r["crawl_time"])) not in seen
                ]
                if todo:
                    cdb.insert(cloud_tbl, todo, context="migrate:crawl_logs")
                after = cdb.count(cloud_tbl)
                print(f"[+] {local_tbl}: 读入 {len(rows)} 行，"
                      f"新增 {len(todo)} 行（{len(rows) - len(todo)} 行已存在），"
                      f"云端 {before} -> {after}")
            else:
                stats = cdb.upsert_history(cloud_tbl, rows,
                                           context=f"migrate:{local_tbl}")
                after = cdb.count(cloud_tbl)
                print(f"[+] {local_tbl}: 读入 {len(rows)} 行，"
                      f"新增 {stats['added']} / 修正 {stats['updated']}，"
                      f"云端 {before} -> {after}")

        grand += len(rows)

    # ---------------- 大表 ----------------
    print("\n--- 大表 ---")
    for cloud_tbl, local_tbl, columns, on_conflict in BIG_MIGRATIONS:
        rows = read_local(local_tbl, columns)
        if not rows:
            print(f"[=] {local_tbl}: 本地无数据，跳过")
            continue

        # 时间列：created_at/updated_at/fetched_at 按 UTC 校正；
        # publish_time 是**本地时间语义**（业务字段，不能当 UTC 处理），
        # _normalize_utc_columns 内部只处理那三个列名，天然排除 publish_time。
        rows = _normalize_utc_columns(rows, columns)

        before = cdb.count(cloud_tbl)
        if on_conflict:
            cdb.upsert(cloud_tbl, rows, on_conflict=on_conflict,
                       context=f"migrate:{local_tbl}")
        else:
            # 无唯一键（company_valuations）：按内容全量对齐，先查已存在的 id 集合
            existing = cdb.select_all(cloud_tbl, columns="*", max_rows=200000,
                                      context=f"migrate:{local_tbl}:read")
            if existing:
                import json as _json
                sig = {_json.dumps({k: r.get(k) for k in columns if k not in
                                    ("created_at", "updated_at")},
                                   sort_keys=True, default=str) for r in existing}
                rows = [r for r in rows
                        if _json.dumps({k: r.get(k) for k in columns if k not in
                                        ("created_at", "updated_at")},
                                       sort_keys=True, default=str) not in sig]
            if rows:
                cdb.insert(cloud_tbl, rows, context=f"migrate:{local_tbl}")
        after = cdb.count(cloud_tbl)
        print(f"[+] {local_tbl}: 读入 {len(rows)} 行，云端 {before} -> {after}")
        grand += len(rows)

    print(f"\n完成，共处理 {grand} 行。")
    cdb.close_client()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())