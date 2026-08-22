import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app import crud
from app.api.login import require_auth

logger = logging.getLogger(__name__)

router = APIRouter()

# 财务指标 显示名 -> FinancialReport 表列名（顺序即展示顺序）
FIELD_COLUMNS = crud.FINANCIAL_FIELD_COLUMNS


def _serialize(records) -> list:
    """将 ORM 记录转为 {报告期, 各财务指标} 字典列表（与抓取返回结构一致）。"""
    rows = []
    for rec in records:
        row = {"报告期": rec.report_date}
        for name, column in FIELD_COLUMNS.items():
            row[name] = getattr(rec, column)
        rows.append(row)
    return rows


def _latest_name(records) -> Optional[str]:
    """取记录中最新的非空股票名称（记录按报告期升序，倒序遍历）。"""
    for rec in reversed(records):
        if getattr(rec, "stock_name", None):
            return rec.stock_name
    return None


def _validate_period(start: Optional[str], end: Optional[str]):
    for label, value in (("start", start), ("end", end)):
        if value is None:
            continue
        parts = value.split("-")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise HTTPException(status_code=400, detail=f"{label} 日期格式应为 YYYY-MM-DD，如 2024-03-31")


@router.get("/finance/query")
async def query_finance(
    code: Optional[str] = Query(None, description="股票代码，如 600519"),
    name: Optional[str] = Query(None, description="股票名称，如 贵州茅台"),
    start: Optional[str] = Query(None, description="起始报告期 YYYY-MM-DD（季度末）"),
    end: Optional[str] = Query(None, description="结束报告期 YYYY-MM-DD（季度末）"),
    db: Session = Depends(get_db),
    auth: bool = Depends(require_auth),
):
    """查询已入库的财务指标（不重新抓取）。支持按股票代码或股票名称查询，至少提供一个。"""
    try:
        if not code and not name:
            raise HTTPException(status_code=400, detail="请提供股票代码或股票名称")
        _validate_period(start, end)
        records = crud.get_financial_reports(
            db, stock_code=code, stock_name=name, start=start, end=end
        )
        return {
            "status": "ok",
            "code": code,
            "name": name or _latest_name(records),
            "fields": list(FIELD_COLUMNS.keys()),
            "records": _serialize(records),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"查询财务指标失败: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@router.post("/finance/fetch")
def fetch_finance(
    code: str = Query(..., description="股票代码，如 600519"),
    name: Optional[str] = Query(None, description="股票名称，如 贵州茅台"),
    start: Optional[str] = Query(None, description="起始报告期 YYYY-MM-DD（季度末）"),
    end: Optional[str] = Query(None, description="结束报告期 YYYY-MM-DD（季度末）"),
    db: Session = Depends(get_db),
    auth: bool = Depends(require_auth),
):
    """从新浪财报抓取指定股票的财务指标，按报告期入库，并返回抓取结果。

    同步抓取（akshare 基于 requests），FastAPI 会将其放入线程池执行，不阻塞事件循环。
    """
    try:
        _validate_period(start, end)
        from getAkshare import fetch_financial_reports, reports_to_records

        df = fetch_financial_reports(code)
        records = reports_to_records(df)
        if start or end:
            records = [
                r for r in records
                if (not start or r["报告期"] >= start) and (not end or r["报告期"] <= end)
            ]
        saved = crud.upsert_financial_reports(db, code, records, stock_name=name)
        logger.info(f"股票 {code}({name or '-'}) 财务指标抓取入库 {saved} 期")
        return {
            "status": "ok",
            "code": code,
            "name": name,
            "saved": saved,
            "fields": list(FIELD_COLUMNS.keys()),
            "records": records,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"抓取财务指标失败: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}
