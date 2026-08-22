"""新浪财报数据抓取：合并三大报表（利润表/资产负债表/现金流量表），提取财务指标。

数据源: akshare.stock_financial_report_sina（新浪财经）。
新浪报表中的日期列名为「报告日」（YYYYMMDD），此处统一重命名为「报告期」并格式化为 YYYY-MM-DD。
"""
import akshare as ak
import pandas as pd

# 财务指标 显示名 -> 新浪报表原始列名
# 注意:新浪报表无「短期理财」原始列，按会计准则通常计入「交易性金融资产」，此处以此近似替代
FIELD_MAP = {
    "营业收入": "营业收入",
    "营业成本": "营业成本",
    "归母净利润": "归属于母公司所有者的净利润",
    "存货": "存货",
    "应收账款": "应收账款",
    "货币资金": "货币资金",
    "短期理财": "交易性金融资产",
    "合同负债": "合同负债",
    "股东权益": "所有者权益(或股东权益)合计",
    "经营活动现金流净额": "经营活动产生的现金流量净额",
    "短期借款": "短期借款",
    "一年内到期的非流动负债": "一年内到期的非流动负债",
    "长期借款": "长期借款",
    "应付债券": "应付债券",
    "利息支出": "利息支出",
}

# 需要入库/展示的财务指标（显示名，顺序即展示顺序）
NEED_COLS = list(FIELD_MAP.keys())


def _normalize_report_date(value) -> str:
    """将报告日统一格式化为 YYYY-MM-DD 字符串（兼容 YYYYMMDD 与 YYYY-MM-DD）。"""
    text = str(value).strip()
    if "-" in text:
        return text[:10]
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text


def fetch_financial_reports(code: str) -> pd.DataFrame:
    """抓取指定股票的三大报表并按报告期合并，仅保留 FIELD_MAP 中的原始列。

    返回 DataFrame：列 = ['报告期'] + NEED_COLS，按报告期升序去重。
    """
    def _load(symbol: str) -> pd.DataFrame:
        df = ak.stock_financial_report_sina(stock=code, symbol=symbol)
        df = df.rename(columns={"报告日": "报告期"})
        df["报告期"] = df["报告期"].map(_normalize_report_date)
        keep = ["报告期"] + [c for c in FIELD_MAP.values() if c in df.columns]
        return df[keep]

    profit = _load("利润表")
    balance = _load("资产负债表")
    cashflow = _load("现金流量表")

    result = profit.merge(balance, on="报告期", how="inner").merge(cashflow, on="报告期", how="inner")
    result = result[["报告期"] + list(FIELD_MAP.values())].copy()
    result = result.rename(columns={v: k for k, v in FIELD_MAP.items()})
    result = result.drop_duplicates(subset=["报告期"], keep="last")
    result = result.sort_values("报告期").reset_index(drop=True)
    return result


def reports_to_records(df: pd.DataFrame) -> list:
    """将合并后的 DataFrame 转为数据库记录列表（NaN 转 None，数值转 float）。"""
    records = []
    for _, row in df.iterrows():
        rec = {"报告期": row["报告期"]}
        for col in NEED_COLS:
            value = row.get(col)
            rec[col] = None if pd.isna(value) else float(value)
        records.append(rec)
    return records
