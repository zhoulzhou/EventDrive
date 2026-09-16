"""GitHub 数据库备份工具。

将本地 SQLite 数据库(同步 WAL 快照)通过 GitHub Contents API 上传到仓库的备份目录,
每次按时间戳命名以保留多个历史备份。
"""
import base64
import logging
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings, BASE_DIR

logger = logging.getLogger(__name__)

GH_API_BASE = "https://api.github.com"


def _db_path() -> Path:
    """解析配置的 SQLite 数据库文件路径(支持相对/绝对路径)。"""
    url = settings.DATABASE_URL
    if url and url.startswith("sqlite:///"):
        rel = url[len("sqlite:///"):]
        p = Path(rel)
        return p if p.is_absolute() else BASE_DIR / p
    return settings.DATA_DIR / "db.sqlite3"


def _create_snapshot() -> bytes:
    """生成数据库一致快照(WAL 模式下合并未落盘的已提交数据)。"""
    src = _db_path()
    if not src.exists():
        raise FileNotFoundError(f"数据库文件不存在: {src}")

    with tempfile.TemporaryDirectory() as tmpdir:
        dst_file = Path(tmpdir) / "snapshot.sqlite3"
        src_conn = sqlite3.connect(str(src), timeout=5)
        dst_conn = sqlite3.connect(str(dst_file))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()
        data = dst_file.read_bytes()
        logger.info(f"数据库快照生成完成, 大小: {len(data):,} bytes")
        return data


def _auth_headers() -> dict:
    if not settings.GITHUB_TOKEN:
        raise PermissionError("未配置 GITHUB_TOKEN, 请在 .env 中填写 GitHub Personal Access Token")
    return {
        "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _backup_remote_path() -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dir_ = settings.GITHUB_BACKUP_DIR.strip("/")
    return f"{dir_}/db_{ts}.sqlite3"


def backup_db_to_github() -> dict:
    """执行数据库备份。返回 {success, message, path?, size?}。"""
    owner = settings.GITHUB_REPO_OWNER.strip("/")
    repo = settings.GITHUB_REPO_NAME.strip("/")
    if not owner or not repo:
        return {"success": False, "message": "未配置 GITHUB_REPO_OWNER / GITHUB_REPO_NAME"}

    try:
        data = _create_snapshot()
    except FileNotFoundError as e:
        return {"success": False, "message": str(e)}
    except Exception as e:
        logger.exception("数据库快照失败")
        return {"success": False, "message": f"生成快照失败: {e}"}

    path = _backup_remote_path()
    url = f"{GH_API_BASE}/repos/{owner}/{repo}/contents/{path}"

    try:
        with httpx.Client(timeout=60.0) as client:
            headers = _auth_headers()

            # 获取已存在文件的 sha(用于更新), 不存在则跳过
            sha: Optional[str] = None
            resp = client.get(url, headers=headers)
            if resp.status_code == 200:
                sha = resp.json().get("sha")
            elif resp.status_code != 404:
                return {
                    "success": False,
                    "message": f"查询远端备份失败: HTTP {resp.status_code} {resp.text[:200]}",
                }

            payload = {
                "message": f"[EventDrive] 备份数据库 {path}",
                "content": base64.b64encode(data).decode("utf-8"),
            }
            if sha:
                payload["sha"] = sha

            resp = client.put(url, json=payload, headers=headers)

        if resp.status_code in (200, 201):
            size = len(data)
            logger.info(f"数据库备份成功: {owner}/{repo}/{path} ({size:,} bytes)")
            return {
                "success": True,
                "message": f"备份成功: {path} ({size:,} bytes)",
                "path": path,
                "size": size,
            }
        return {
            "success": False,
            "message": f"上传失败: HTTP {resp.status_code} {resp.text[:300]}",
        }
    except PermissionError as e:
        return {"success": False, "message": str(e)}
    except Exception as e:
        logger.exception("数据库备份失败")
        return {"success": False, "message": f"备份失败: {e}"}