import logging
from typing import Optional
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
    forecast_years: int  # 高增长期年限（年）
    growth_forecast: float  # 高增长期增长率（%）
    transition_years: int  # 放缓期年限（年）
    growth_transition: float  # 放缓期增长率（%）
    growth_perpetual: float  # 永续增长率（%）
    discount_rate: float  # 折现率（%）
    current_market_value: Optional[float] = None  # 当前市值（亿元），仅用于记录展示，不参与计算

    @field_validator("company_name")
    @classmethod
    def _name_not_blank(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("企业名称不能为空")
        return v

    @field_validator("base_profit", "growth_forecast", "growth_transition", "growth_perpetual", "discount_rate")
    @classmethod
    def _valid_number(cls, v):
        return float(v)

    @field_validator("forecast_years")
    @classmethod
    def _valid_years(cls, v):
        v = int(v)
        if v < 1:
            raise ValueError("高增长期年限需大于 0")
        return v

    @field_validator("transition_years")
    @classmethod
    def _valid_transition_years(cls, v):
        v = int(v)
        if v < 0:
            raise ValueError("放缓期年限不能为负")
        return v


def _compute_valuation(input: ValuationInput) -> float:
    """三阶段 DCF 估值：净利润替代 FCFF。

    高增长期（forecast_years 年，增速 growth_forecast）→ 放缓期（transition_years 年，
    增速 growth_transition）→ 永续期（增速 growth_perpetual）。逐年预测净利润并折现加总，
    终值为放缓期末净利润按永续增长率折现。
    """
    base_profit = input.base_profit
    high_years = input.forecast_years
    transition_years = input.transition_years
    growth_high = input.growth_forecast / 100.0
    growth_transition = input.growth_transition / 100.0
    growth_perpetual = input.growth_perpetual / 100.0
    discount_rate = input.discount_rate / 100.0

    # 折现率需大于永续增长率，否则自动上调（与页面提示一致）
    if discount_rate <= growth_perpetual:
        discount_rate = growth_perpetual + 0.03

    total_pv = 0.0

    # 阶段一：高增长期，净利润按 growth_high 逐年增长
    for year in range(1, high_years + 1):
        profit_year = base_profit * (1 + growth_high) ** year
        total_pv += profit_year / (1 + discount_rate) ** year

    # 阶段二：放缓期，从高增长期末净利润起按 growth_transition 逐年增长
    profit_end_high = base_profit * (1 + growth_high) ** high_years
    profit = profit_end_high
    for year in range(1, transition_years + 1):
        profit = profit_end_high * (1 + growth_transition) ** year
        total_pv += profit / (1 + discount_rate) ** (high_years + year)

    # 阶段三：永续期，以放缓期末净利润为基准按永续增长率折现
    last_profit = profit
    terminal_value = last_profit * (1 + growth_perpetual) / (discount_rate - growth_perpetual)
    terminal_pv = terminal_value / (1 + discount_rate) ** (high_years + transition_years)
    return total_pv + terminal_pv


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
                transition_years=input.transition_years,
                growth_transition=round(input.growth_transition, 2),
                growth_perpetual=round(input.growth_perpetual, 2),
                discount_rate=round(input.discount_rate, 2),
                current_market_value=(
                    round(input.current_market_value, 2)
                    if input.current_market_value is not None
                    else None
                ),
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