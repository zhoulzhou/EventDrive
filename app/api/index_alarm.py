import logging
from pathlib import Path
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import BASE_DIR
from app.database import get_db
from app import crud
from app.api.login import require_auth

logger = logging.getLogger(__name__)

router = APIRouter()


def _parse_csv(path: Path):
    """解析 index/ 下的 CSV（DATE,VALUE），空值跳过，返回 {date: float}。"""
    result = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        for line in lines[1:]:  # 跳过表头
            comma = line.find(",")
            if comma < 0:
                continue
            date = line[:comma].strip()
            val = line[comma + 1:].strip()
            if not date or not val:
                continue
            try:
                result[date] = float(val)
            except ValueError:
                continue
    except Exception as e:
        logger.error(f"解析 CSV {path.name} 失败: {e}")
    return result


def _import_index_csv(db: Session):
    """将 index/ 下的 CSV 数据按日期写入 index_history 宽表（列名取 CSV 文件名）。"""
    index_dir = BASE_DIR / "index"
    rows = []
    for filename, column in crud.INDEX_CSV_COLUMNS.items():
        path = index_dir / filename
        if not path.exists():
            logger.warning(f"缺少指数预警数据文件: {path}")
            continue
        for date, value in _parse_csv(path).items():
            rows.append({"date": date, "column": column, "value": value})
    if rows:
        count = crud.upsert_index_history_rows(db, rows)
        logger.info(f"已导入指数预警 CSV 数据，共 {count} 个日期")
    else:
        logger.warning("指数预警 CSV 数据为空，未导入任何记录")


# CSV 解析结果的内存缓存。key 用「文件名 + mtime_ns + size」而不是时间戳：
# CSV 是随代码发布的静态文件，更新时 mtime 必变 —— 这样既能「改了立刻生效」，
# 又不必每次请求重新解析 215KB 文本。
_csv_cache: dict = {"sig": None, "points": None}


def _csv_signature():
    """4 个 CSV 的 (文件名, mtime_ns, size) 签名；任一缺失则返回 None。"""
    sig = []
    for filename in crud.INDEX_CSV_COLUMNS:
        path = BASE_DIR / "index" / filename
        try:
            st = path.stat()
        except OSError:
            return None
        sig.append((filename, st.st_mtime_ns, st.st_size))
    return tuple(sig)


def load_index_points_from_csv():
    """直接从 index/ CSV 组装 points，文件缺失或解析为空时返回 None。

    为什么加这一层：这张表的**唯一来源就是 index/ 下的 4 个 CSV**，数据库只是
    中间层。实测云库路径冷启动 1.1 秒（2967 行要分 3 次翻页，每次往返 0.25~0.9 秒），
    而直接读 CSV 只要 0.035 秒，且不随网关波动。命中缓存后更是微秒级。

    顺带解决了老逻辑的一个死角：原先靠 `RELOAD_INDEX_DATA` 环境变量触发重导，
    但发布工具无法注入环境变量，线上 CSV 更新后根本没有办法刷进数据库。
    改成「CSV 优先 + mtime 缓存」后，换掉 CSV 文件即自动生效。
    """
    sig = _csv_signature()
    if sig is None:
        return None
    if _csv_cache["sig"] == sig and _csv_cache["points"] is not None:
        return _csv_cache["points"]

    merged = {}
    for filename, column in crud.INDEX_CSV_COLUMNS.items():
        for date, value in _parse_csv(BASE_DIR / "index" / filename).items():
            merged.setdefault(date, {"date": date})[column] = value
    if not merged:
        return None

    # 日期是 YYYY-MM-DD，字符串排序即时间排序，与数据库的 order(date.asc) 一致
    points = [merged[d] for d in sorted(merged)]
    _csv_cache["sig"] = sig
    _csv_cache["points"] = points
    return points


def ensure_index_data_loaded() -> bool:
    """应用启动时调用:仅当 index_history 表为空时,从 index/ CSV 一次性导入。

    注意：接口已改为优先读 CSV（见 load_index_points_from_csv），这里导入的数据
    只在 CSV 缺失时作为降级数据源使用，不再参与日常读取。
    """
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        if crud.get_index_history_count(db) == 0:
            _import_index_csv(db)
            db.expire_all()
            return True
        return False
    except Exception as e:
        logger.error(f"指数预警 CSV 导入失败: {e}", exc_info=True)
        return False
    finally:
        db.close()


def reload_index_data() -> bool:
    """强制重导:清空 index_history 表后从 index/ CSV 重新导入,并重置内存缓存。

    用于 index/ 下 CSV 文件更新后,通过环境变量 RELOAD_INDEX_DATA=1 在启动时触发。
    """
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        deleted = crud.delete_all_index_history(db)
        _import_index_csv(db)
        db.expire_all()
        logger.info(f"指数预警 CSV 已强制重导,清空 {deleted} 条旧记录")
        return True
    except Exception as e:
        logger.error(f"指数预警 CSV 强制重导失败: {e}", exc_info=True)
        return False
    finally:
        db.close()


@router.get("/index-alarm")
async def get_index_alarm_data(db: Session = Depends(get_db), auth: bool = Depends(require_auth)):
    """返回指数预警全部历史数据(宽表形式 date -> 各列值)。

    数据源**优先 index/ 下的 CSV**（按 mtime 缓存，命中后零 IO），因为那才是原始出处；
    CSV 缺失或解析为空时才降级查 index_history 表。响应里的 `source` 字段标明实际来源，
    便于线上排查走的是哪条路。
    """
    try:
        points = load_index_points_from_csv()
        source = "csv"
        if points is None:
            rows = crud.get_index_history_all(db)
            points = []
            for row in rows:
                point = {"date": row.date}
                for col in crud.INDEX_COLUMN_NAMES:
                    val = getattr(row, col)
                    if val is not None:
                        point[col] = val
                points.append(point)
            source = "db"

        return {
            "status": "ok",
            "columns": crud.INDEX_COLUMN_NAMES,
            "source": source,
            "points": points,
        }
    except Exception as e:
        logger.error(f"获取指数预警数据失败: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}