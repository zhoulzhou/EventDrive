from fastapi import APIRouter, Depends

from app.api.login import require_auth
from app.utils.github_backup import backup_db_to_github

router = APIRouter()


@router.post("/backup/db")
def backup_db(auth: bool = Depends(require_auth)):
    """将本地数据库备份到 GitHub 仓库, 返回 {success, message, path?, size?}。"""
    return backup_db_to_github()