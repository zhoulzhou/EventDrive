from sqlalchemy import Column, Integer, String, Text, DateTime, Float, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class News(Base):
    __tablename__ = "news"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    title = Column(Text, nullable=False)
    content = Column(Text, nullable=True)
    source = Column(Text, nullable=False)
    publish_time = Column(DateTime, nullable=False)
    url = Column(Text, unique=True, nullable=False)
    author = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)
    image_path = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class MarketPrice(Base):
    __tablename__ = "market_prices"
    __table_args__ = (UniqueConstraint("symbol", "date", name="uq_market_symbol_date"),)

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    symbol = Column(Text, nullable=False, index=True)
    name = Column(Text, nullable=False)
    unit = Column(Text, nullable=True)
    value = Column(Float, nullable=True)
    date = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class MarketStrategyState(Base):
    """市场行情峰值回撤策略状态：持久化每个指标的当前峰值，跨天/跨重启生效。

    - peak_value / peak_date: 当前追踪峰值（新高自动上移，回撤达阈值时重置为当前值）
    - drawdown_date: 最近一次触发回撤预警（-red_pct%）的日期，用于该日显示红色预警
    """
    __tablename__ = "market_strategy_state"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    symbol = Column(Text, nullable=False, unique=True, index=True)
    peak_value = Column(Float, nullable=False)
    peak_date = Column(Text, nullable=False)
    drawdown_date = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class IndexHistory(Base):
    """指数预警历史宽表：一行一个日期，每列对应 index/ 下一个 CSV 文件（列名取 CSV 文件名）。"""
    __tablename__ = "index_history"
    __table_args__ = (UniqueConstraint("date", name="uq_index_history_date"),)

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    date = Column(Text, nullable=False, index=True)
    NASDAQCOM_2015 = Column(Float, nullable=True)
    VIXCLS_2015 = Column(Float, nullable=True)
    DGS2_2015 = Column(Float, nullable=True)
    DGS10_2015 = Column(Float, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class CompanyValuation(Base):
    """公司估值（DCF）：按 企业名称 存储一次估值计算记录。

    数据来源: 估值计算页面前端输入，由本应用的估值 API 计算并入库。
    以自增 id 为主键，按 created_at 倒序展示最近 100 条。
    """
    __tablename__ = "company_valuations"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    company_name = Column(Text, nullable=False)  # 企业名称
    base_profit = Column(Float, nullable=False)  # 基期净利润（亿元）
    forecast_years = Column(Integer, nullable=False)  # 预测期年限（年）
    growth_forecast = Column(Float, nullable=False)  # 预测期增长率（%）
    growth_perpetual = Column(Float, nullable=False)  # 永续增长率（%）
    discount_rate = Column(Float, nullable=False)  # 折现率（%）
    enterprise_value = Column(Float, nullable=False)  # 企业整体价值（亿元）
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class MacroIndicator(Base):
    """市场指标（资金面 / 经济热度）最新读数：一行一个指标。

    - key: 指标键，与 app/utils/macro_indicators.py 中的 INDICATORS / PARAM_META 对应
    - value / as_of: 当前读数与数据日期（数据发布日，如 2026-09-21 / 2026-08 / 2026-06）
    - source: 'akshare' 行情类自动抓取 / 'pbc' 央行官网自动抓取 / 'fallback' 抓取失败时的兜底默认值
    - detail: 读数明细（JSON 字符串，如 DR007 与逆回购利率的分解、M1/M2 同比、公告号与链接）

    本表只保留每个指标的最新一行（覆盖式）。完整历史见 MacroIndicatorHistory。
    """
    __tablename__ = "macro_indicators"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    key = Column(Text, nullable=False, unique=True, index=True)
    value = Column(Float, nullable=True)
    as_of = Column(Text, nullable=True)
    source = Column(Text, nullable=False, default="akshare")
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class MacroIndicatorHistory(Base):
    """市场指标历史读数：只增不改，用于回溯任一指标的变化。

    每次抓取后，若某指标的读数（as_of 或 value）与上一条历史记录不同，就追加一行；
    连续相同的读数不重复记录，避免周期性刷新堆出大量无意义的重复行。
    fetched_at 为该读数**首次**被抓到的时刻。

    - key / value / as_of / source / detail：同 MacroIndicator
    - fetched_at: 抓取时间（服务器本地时间）
    """
    __tablename__ = "macro_indicator_history"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    key = Column(Text, nullable=False, index=True)
    value = Column(Float, nullable=True)
    as_of = Column(Text, nullable=True)
    source = Column(Text, nullable=False, default="akshare")
    detail = Column(Text, nullable=True)
    fetched_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)


class FinancialReport(Base):
    """财务指标：按 股票代码 + 报告期 存储公司三大报表关键科目（金额单位：元）。

    数据来源: 新浪财报（akshare.stock_financial_report_sina），由财务指标页面抓取入库。
    以股票代码为查询主键，同一股票可有多期记录（报告期 YYYY-MM-DD）。
    """
    __tablename__ = "financial_reports"
    __table_args__ = (UniqueConstraint("stock_code", "report_date", name="uq_financial_stock_date"),)

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    stock_code = Column(Text, nullable=False, index=True)
    stock_name = Column(Text, nullable=True)  # 股票名称
    report_date = Column(Text, nullable=False)  # 报告期 YYYY-MM-DD
    # 利润表
    revenue = Column(Float, nullable=True)  # 营业收入
    operating_cost = Column(Float, nullable=True)  # 营业成本
    net_profit = Column(Float, nullable=True)  # 归母净利润
    # 资产负债表
    inventory = Column(Float, nullable=True)  # 存货
    accounts_receivable = Column(Float, nullable=True)  # 应收账款
    cash = Column(Float, nullable=True)  # 货币资金
    short_term_investment = Column(Float, nullable=True)  # 短期理财
    contract_liabilities = Column(Float, nullable=True)  # 合同负债
    shareholders_equity = Column(Float, nullable=True)  # 股东权益
    # 现金流量表
    operating_cash_flow = Column(Float, nullable=True)  # 经营活动现金流净额
    short_term_borrowing = Column(Float, nullable=True)  # 短期借款
    non_current_liab_due_1y = Column(Float, nullable=True)  # 一年内到期的非流动负债
    long_term_borrowing = Column(Float, nullable=True)  # 长期借款
    bonds_payable = Column(Float, nullable=True)  # 应付债券
    interest_expense = Column(Float, nullable=True)  # 利息支出
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
