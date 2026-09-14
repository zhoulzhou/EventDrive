import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy.orm import Session

from app.database import get_db
from app import crud, schemas
from app.api.login import require_auth

logger = logging.getLogger(__name__)

router = APIRouter()


class ValuationInput(BaseModel):
    """估值计算输入参数（百分比直接以百分数值传入，如 10 表示 10%）。"""
    model_config = ConfigDict(extra="forbid")

    company_name: str
    base_profit: float  # 基期净利润（亿元）
    forecast_years: int  # 预测期年限（年）
    growth_forecast: float  # 预测期增长率（%）
    growth_perpetual: float  # 永续增长率（%）
    discount_rate: float  # 折现率（%）

    @field_validator("company_name")
    @classmethod
    def _name_not_blank(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("企业名称不能为空")
        return v

    @field_validator("base_profit", "growth_forecast", "growth_perpetual", "discount_rate")
    @classmethod
    def _valid_number(cls, v):
        return float(v)

    @field_validator("forecast_years")
    @classmethod
    def _valid_years(cls, v):
        v = int(v)
        if v < 1:
            raise ValueError("预测期年限需大于 0")
        return v


def _compute_valuation(input: ValuationInput) -> float:
    """DCF 估值：净利润替代 FCFF。

    与参考页面算法一致：逐年预测净利润并折现，加总；终值为末年末净利按永续增长率折现。
    """
    base_profit = input.base_profit
    forecast_years = input.forecast_years
    growth_forecast = input.growth_forecast / 100.0
    growth_perpetual = input.growth_perpetual / 100.0
    discount_rate = input.discount_rate / 100.0

    # 折现率需大于永续增长率，否则自动上调（与页面提示一致）
    if discount_rate <= growth_perpetual:
        discount_rate = growth_perpetual + 0.03

    total_forecast_pv = 0.0
    last_profit = 0.0
    for year in range(1, forecast_years + 1):
        profit_year = base_profit * (1 + growth_forecast) ** year
        discount_factor = 1 / (1 + discount_rate) ** year
        total_forecast_pv += profit_year * discount_factor
        if year == forecast_years:
            last_profit = profit_year

    terminal_value = last_profit * (1 + growth_perpetual) / (discount_rate - growth_perpetual)
    terminal_pv = terminal_value / (1 + discount_rate) ** forecast_years
    return total_forecast_pv + terminal_pv


@router.post("/valuation/calculate")
async def calculate_valuation(
    input: ValuationInput,
    db: Session = Depends(get_db),
    auth: bool = Depends(require_auth),
):
    """计算企业整体价值并写入数据库历史记录，返回计算结果及最新历史列表。"""
    try:
        value = _compute_valuation(input)
        record = crud.create_valuation_record(
            db,
            schemas.CompanyValuationCreate(
                company_name=input.company_name,
                base_profit=round(input.base_profit, 2),
                forecast_years=input.forecast_years,
                growth_forecast=round(input.growth_forecast, 2),
                growth_perpetual=round(input.growth_perpetual, 2),
                discount_rate=round(input.discount_rate, 2),
                enterprise_value=round(value, 2),
            ),
        )
        records = crud.get_valuation_records(db)
        return {
            "status": "ok",
            "id": record.id,
            "enterprise_value": round(value, 2),
            "history": [schemas.CompanyValuation.model_validate(r) for r in records],
        }
    except Exception as e:
        logger.error(f"估值计算失败: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@router.get("/valuation/history")
async def list_valuation_history(
    limit: int = Query(100, ge=1, le=100, description="返回条数，默认 100"),
    db: Session = Depends(get_db),
    auth: bool = Depends(require_auth),
):
    """返回估算历史记录（按时间倒序，最新在前）。"""
    try:
        records = crud.get_valuation_records(db, limit=limit)
        return {
            "status": "ok",
            "history": [schemas.CompanyValuation.model_validate(r) for r in records],
        }
    except Exception as e:
        logger.error(f"查询估值历史失败: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@router.delete("/valuation/{record_id}")
async def delete_valuation(record_id: int, db: Session = Depends(get_db), auth: bool = Depends(require_auth)):
    """按 id 删除一条估值记录。"""
    try:
        ok = crud.delete_valuation_record(db, record_id)
        if not ok:
            raise HTTPException(status_code=404, detail="记录不存在")
        return {"status": "ok", "deleted": record_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除估值记录失败: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@router.delete("/valuation/history")
async def clear_valuation_history(db: Session = Depends(get_db), auth: bool = Depends(require_auth)):
    """清空所有估算历史记录。"""
    try:
        deleted = crud.clear_valuation_records(db)
        return {"status": "ok", "deleted": deleted}
    except Exception as e:
        logger.error(f"清空估值历史失败: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}