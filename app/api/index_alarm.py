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


def ensure_index_data_loaded() -> bool:
    """应用启动时调用:仅当 index_history 表为空时,从 index/ CSV 一次性导入。"""
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
    """返回指数预警全部历史数据(从数据库读取,宽表形式 date -> 各列值)。"""
    try:
        rows = crud.get_index_history_all(db)
        points = []
        for row in rows:
            point = {"date": row.date}
            for col in crud.INDEX_COLUMN_NAMES:
                val = getattr(row, col)
                if val is not None:
                    point[col] = val
            points.append(point)

        return {"status": "ok", "columns": crud.INDEX_COLUMN_NAMES, "points": points}
    except Exception as e:
        logger.error(f"获取指数预警数据失败: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}