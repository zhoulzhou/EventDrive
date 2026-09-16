"""数据库本地备份工具。

将当前 SQLite 数据库(同步 WAL 快照)复制到项目根目录 backup/ 下,
按时间戳命名以区分历史备份。备份文件纳入 git 管理, 由用户手动提交。
"""
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from app.config import settings, BASE_DIR

logger = logging.getLogger(__name__)


def _db_path() -> Path:
    """解析配置的 SQLite 数据库文件路径(支持相对/绝对路径)。"""
    url = settings.DATABASE_URL
    if url and url.startswith("sqlite:///"):
        rel = url[len("sqlite:///"):]
        p = Path(rel)
        return p if p.is_absolute() else BASE_DIR / p
    return settings.DATA_DIR / "db.sqlite3"


def local_backup_db() -> dict:
    """复制数据库到 backup/ 目录。返回 {success, message, filename?, path?}。"""
    src = _db_path()
    if not src.exists():
        return {"success": False, "message": f"数据库文件不存在: {src}"}

    settings.DB_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = settings.DB_BACKUP_DIR / f"db_{ts}.sqlite3"

    try:
        # 用 sqlite3 backup 生成一致性快照, 避免 WAL 未落盘时直接复制丢数据
        src_conn = sqlite3.connect(str(src), timeout=5)
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()

        size = dst.stat().st_size
        rel = dst.relative_to(BASE_DIR)
        logger.info(f"数据库本地备份成功: {rel} ({size:,} bytes)")
        return {
            "success": True,
            "message": f"已备份到 {rel} ({size:,} bytes), 可在本地提交到 git",
            "filename": dst.name,
            "path": str(rel),
        }
    except Exception as e:
        logger.exception("数据库本地备份失败")
        return {"success": False, "message": f"备份失败: {e}"}