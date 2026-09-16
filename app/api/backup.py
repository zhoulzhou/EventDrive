from fastapi import APIRouter, Depends

from app.api.login import require_auth
from app.utils.local_backup import local_backup_db

router = APIRouter()


@router.post("/backup/db")
def backup_db(auth: bool = Depends(require_auth)):
    """将当前数据库复制到本地 backup/ 目录, 返回 {success, message, filename?, path?}。"""
    return local_backup_db()