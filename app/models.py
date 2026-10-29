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
    forecast_years = Column(Integer, nullable=False)  # 高增长期年限（年）
    growth_forecast = Column(Float, nullable=False)  # 高增长期增长率（%）
    transition_years = Column(Integer, nullable=True)  # 【已下线 2026-10-25】放缓期年限（年），列保留仅存历史行，不再写入/展示
    growth_transition = Column(Float, nullable=True)  # 【已下线 2026-10-25】放缓期增长率（%），同上
    growth_perpetual = Column(Float, nullable=False)  # 永续增长率（%）
    discount_rate = Column(Float, nullable=False)  # 折现率（%）
    current_market_value = Column(Float, nullable=True)  # 当前市值（亿元），仅用于记录展示，不参与计算
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


class StockTempIndicator(Base):
    """股票指标（A 股冷热三层温度计）最新读数：一行一个指标。

    与 MacroIndicator 结构一致，但独立成表：股票指标口径是「估值 / 情绪 / 资金 / 结构」
    四组，与宏观指标的「资金面 / 经济热度」两组的抓取源、频率完全不同，混在一张表里
    会让两边的「待刷新判定」「缓存有效期」互相干扰。

    - key: 指标键，与 app/utils/stock_temp.py 的 INDICATORS 对应
    - value / as_of: 当前读数与数据日期
    - source: 'akshare' 自动抓取（含交易所官网、乐咕乐股、东财数据中心、新浪）
    - detail: 读数明细（JSON 字符串，含十年分位、分位窗口、分量分解等）

    本表只保留每个指标的最新一行（覆盖式）。完整历史见 StockTempHistory。
    """
    __tablename__ = "stock_temp_indicators"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    key = Column(Text, nullable=False, unique=True, index=True)
    value = Column(Float, nullable=True)
    as_of = Column(Text, nullable=True)
    source = Column(Text, nullable=False, default="akshare")
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class StockTempHistory(Base):
    """股票指标历史读数：只增不改，用于趋势图与分位计算。

    写入口径与 MacroIndicatorHistory 完全一致：按 (key, as_of) 唯一定位，
    已存在且数值相同则跳过，数值不同则就地修正，避免反复回填堆出重复点。
    """
    __tablename__ = "stock_temp_history"

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


class LhbRecord(Base):
    """龙虎榜榜单：一行 = 一个交易日 × 一只股票 × 一个上榜原因（东财 TRADE_ID 唯一）。

    口径见 docs/龙虎榜.txt 与 docs/龙虎榜案例.txt：
    - 榜单本身只披露买卖前五席位，**是收盘后的滞后快照**，不是买入信号；
    - 单日榜与三日榜（连续三个交易日）**必须分开看**，故 scope 单独存；
    - 资金结构三件事：龙虎净额 / 龙虎成交占比 / 买一集中度；
    - 席位构成、位置、打分与告警在抓取时算好落库，页面只读不算。

    字段来源：code~deal_ratio 来自东财榜单接口；buy1_ratio 及之后为本地计算。
    """

    __tablename__ = "lhb_records"
    __table_args__ = (UniqueConstraint("trade_date", "trade_id", name="uq_lhb_record"),)

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    trade_date = Column(Text, nullable=False, index=True)      # 交易日 YYYY-MM-DD
    trade_id = Column(Text, nullable=False)                    # 东财记录 id
    code = Column(Text, nullable=False, index=True)
    name = Column(Text, nullable=True)
    market = Column(Text, nullable=True)                       # SH / SZ / BJ
    change_type = Column(Text, nullable=True)                  # 东财异动类型码
    explanation = Column(Text, nullable=True)                  # 上榜原因原文
    scope = Column(Text, nullable=True)                        # day / 3day

    close = Column(Float, nullable=True)
    change_rate = Column(Float, nullable=True)                 # 当日涨跌幅 %
    turnover_rate = Column(Float, nullable=True)               # 换手率 %
    free_mcap = Column(Float, nullable=True)                   # 流通市值
    accum_amount = Column(Float, nullable=True)                # 当日总成交额
    buy_amt = Column(Float, nullable=True)                     # 买五合计买入
    sell_amt = Column(Float, nullable=True)                    # 卖五合计卖出
    net_amt = Column(Float, nullable=True)                     # 龙虎净额
    deal_ratio = Column(Float, nullable=True)                  # 龙虎成交占比 %

    buy1_ratio = Column(Float, nullable=True)                  # 买一集中度 %
    inst_net = Column(Float, nullable=True)                    # 机构净买入
    north_net = Column(Float, nullable=True)                   # 北向净买入
    hot_net = Column(Float, nullable=True)                     # 游资净买入
    seat_types = Column(Text, nullable=True)                   # JSON：买卖榜类型分布
    combo_tags = Column(Text, nullable=True)                   # JSON：[组合标签, 说明]
    score = Column(Integer, nullable=True)                     # 0-100
    grade = Column(Text, nullable=True)                        # 档位文案
    alerts = Column(Text, nullable=True)                       # JSON：告警数组
    reasons = Column(Text, nullable=True)                      # JSON：加减分明细
    position = Column(Text, nullable=True)                     # 低位 / 中位 / 高位
    pos_pct = Column(Float, nullable=True)                     # 近 20 日涨幅 %
    streak = Column(Integer, nullable=True)                    # 截至当日连板数
    fetched_at = Column(DateTime, nullable=False, server_default=func.now())


class LhbSeat(Base):
    """龙虎榜席位明细：一行 = 一条榜单记录 × 买/卖 × 名次（前五）。

    dept_name 是营业部全名（机构专用/股通专用为交易所统一名），
    seat_type / seat_tags 由 app/utils/lhb.py 的席位字典打标后落库。
    """

    __tablename__ = "lhb_seats"
    __table_args__ = (
        UniqueConstraint("trade_date", "trade_id", "flag", "seq", name="uq_lhb_seat"),
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    trade_date = Column(Text, nullable=False, index=True)
    trade_id = Column(Text, nullable=False, index=True)
    code = Column(Text, nullable=False, index=True)
    name = Column(Text, nullable=True)
    flag = Column(Text, nullable=False)                        # buy / sell
    seq = Column(Integer, nullable=False)                      # 1-5
    dept_code = Column(Text, nullable=True)
    dept_name = Column(Text, nullable=True)
    buy = Column(Float, nullable=True)
    sell = Column(Float, nullable=True)
    net = Column(Float, nullable=True)
    seat_type = Column(Text, nullable=True)                    # inst/north/hot_money/...
    seat_tags = Column(Text, nullable=True)                    # JSON
    fetched_at = Column(DateTime, nullable=False, server_default=func.now())


class CrawlLog(Base):
    """抓取任务日志：每轮调度 / 手动触发写入一条，便于复盘与排查。"""

    __tablename__ = "crawl_logs"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    # 任务标识：news / market / macro / stock_temp / finance / all
    task = Column(Text, nullable=False, index=True)
    # 轮次：morning(10:00) / close(16:00) / manual(手动触发)
    session = Column(Text, nullable=True, index=True)
    # 数据域：东方财富 / 同花顺 / 新浪财经 / market / macro / ...
    source = Column(Text, nullable=True)
    # 本次该来源抓取到的条数
    item_count = Column(Integer, nullable=False, default=0)
    status = Column(Text, nullable=False, default="ok")  # ok / failed / skipped
    error_message = Column(Text, nullable=True)
    duration = Column(Integer, nullable=False, default=0)  # 秒
    crawl_time = Column(DateTime, nullable=False, index=True, server_default=func.now())
