"""今日热点：把一个交易日的「盘面事实」聚合成一份日报。

设计要点
--------
**为什么是「整份快照」而不是「页面实时拼」**
    页面要同时展示定调、主线、榜单、资金地图、时间轴五块内容，涉及 6 类数据源。
    实时拼意味着每次打开页面都打 5~6 个外部接口 —— 早上 9:15 与晚上 21:34 之间
    任何一次打开都会重抓一遍，既慢又容易被东财限流（实测短时间密集请求会被直接
    掐断连接）。而这份数据**每天只变一次**，所以收盘轮算完存成一条 JSON，
    页面只读库。这也顺带满足了「单响应 ≤1000 行 + 60s 网关」的硬约束。

**数据源（全部东财，直连 HTTP，不依赖 akshare）**
    1. push2ex getTopicZTPool   涨停池：所属行业、连板数、封单额、首封时间
    2. push2ex getTopicDTPool   跌停池
    3. push2ex getTopicZBPool   炸板池（算封板率）
    4. push2   clist            行业 / 概念板块主力资金流
    5. push2   clist            个股主力资金流（流入 / 流出两端）
    6. 库内 news                当日新闻（题材匹配 + 时间轴）
    7. 库内 stock_temp_indicators  成交额 / 市场宽度 / 综合温度
    8. 库内 market_prices       上证指数 / 科创50 收盘与涨跌幅

    为什么不用 akshare 的 stock_zt_pool_em：它内部同样打 push2ex，却把
    「限流后返回空」包装成正常空结果，排查时分不清「今天没涨停」和「源挂了」。
    自己请求能明确区分异常，并做退避重试。

**限流**
    东财 push2 家族短时间密集请求会直接 RemoteDisconnected（实测），所以：
    串行抓取 + 每次请求之间固定间隔 + 失败指数退避重试 3 次。
    整套跑下来约 6~10 秒，远低于收盘轮的预算。

**口径说明（页面上也要写，别让人把「代理指标」当官方数据）**
    · 主线 = 涨停池按细分行业聚簇后归并到大方向，家数最多的前几个
    · 晋级率 = 今日连板（≥2 板）家数 ÷ 昨日涨停家数
    · 封板率 = 涨停家数 ÷（涨停 + 炸板）
    · 「阶段」需要前一日日报做对比，上线首日缺基线时留空，不做假标签
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from app import crud
from app.utils.anti_crawl import get_random_headers

logger = logging.getLogger(__name__)


class HotspotError(RuntimeError):
    """热点抓取失败（区别于「抓到了但为空」）。"""


# ================================================================ 常量

EM_UT = "7eea3edcaed734bea9cbfc24409ed989"
EM_ZT_URL = "https://push2ex.eastmoney.com/getTopicZTPool"
EM_DT_URL = "https://push2ex.eastmoney.com/getTopicDTPool"
EM_ZB_URL = "https://push2ex.eastmoney.com/getTopicZBPool"
EM_CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EM_REFERER = "https://quote.eastmoney.com/"

# clist 的备用域名。三个域名都返回同样的数据结构，只是 CDN 节点不同。
# 为什么要备：push2 对 ``clist/get`` 这个路径有单独的风控（实测被掐时其它路径
# 如 ulist.np 仍然 200，换个域名也能救回来），而涨停池所在的 push2ex 不受影响
# —— 这就是「涨停池抓得到、资金流抓不到」这种半残状态的来源。
EM_CLIST_HOSTS = (
    "https://push2.eastmoney.com",
    "https://push2delay.eastmoney.com",
    "https://push2his.eastmoney.com",
)

# 沪深 A 股（沪主板 + 深主板 + 创业板 + 科创板）
FS_ALL_A = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"

# 请求间隔（秒）与重试策略
#   push2 家族有 IP 级频控：短时间内十几次请求会被直接掐连接（实测连续请求后
#   连 curl 都返回 000，几分钟后才恢复）。而整套只有 7 次请求、每天跑一次，
#   多等几秒完全划算，所以间隔给足、退避给长。
_REQ_GAP = 0.8
_TIMEOUT = 8
_RETRIES = 4
_BACKOFF = 1.5

# 抓取阶段的总预算（秒）。7 次请求正常 10~20 秒；但如果碰上「连接不拒也不回」
# 的超时场景，7 × 超时 × 重试会直接顶穿反代 60 秒，页面变成 504。
# 到点就放弃剩余数据源、记进 data_gaps —— 宁可少一块内容，也不能让接口挂掉。
_BUDGET_SECONDS = 42

_session: Optional[requests.Session] = None


def _http() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        _session = s
    return _session


def _fetch_json(url: str, params: Dict[str, Any], *, what: str,
                retries: int = _RETRIES, expect_list: bool = False,
                referer: str = ""):
    """带退避重试的 JSON 请求。

    东财失败表现为「连接被对端直接关闭」（RemoteDisconnected）而不是 HTTP 错误码，
    所以 except 里要连 ConnectionError / ProtocolError 一起兜住，不能只看 status_code。

    ``expect_list=True`` 用于新浪那类直接返回 JSON 数组的接口。
    """
    last = "未知错误"
    for attempt in range(retries):
        try:
            resp = _http().get(url, params=params, timeout=_TIMEOUT,
                               headers=get_random_headers(referer or EM_REFERER))
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) or (expect_list and isinstance(data, list)):
                    return data
                last = "响应结构不是预期的 JSON"
            else:
                last = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001 —— 连接类异常全兜
            last = f"{type(exc).__name__}: {str(exc)[:100]}"
        if attempt < retries - 1:
            time.sleep(_BACKOFF * (attempt + 1))
    raise HotspotError(f"{what} 抓取失败（{retries} 次）：{last}")


# ---------------------------------------------------------------- 行业归并
#
# 涨停池的 hybk 是东财**二级行业**，且被截断到 4 个汉字（"汽车零部"、"自动化设"）。
# 直接拿它当题材名会很碎：今天 125 家涨停散在 40 多个细分行业里，
# 「广告营销 9 / 出版 8 / 游戏 7 / 影视院线 7 / 数字媒体 5」本该是**一条**传媒主线。
# 所以先归并到大方向，细分行业名仍然保留在卡片里作为证据（否则信息被吃掉）。
#
# 匹配用前缀双向判断（行业名被截断，靠 startswith 兜），顺序即优先级，具体的放前面。
SECTOR_GROUPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("半导体", ("半导体", "分立器件", "集成电路")),
    ("电子", ("元件", "光学光电子", "消费电子", "其他电子", "电子化学品")),
    ("传媒", ("广告营销", "出版", "游戏", "影视院线", "数字媒体", "电视广播", "社交")),
    ("计算机", ("软件开发", "IT服务", "计算机设备")),
    ("通信", ("通信设备", "通信服务")),
    ("医药", ("化学制药", "中药", "生物制品", "医疗器械", "医疗服务", "医药商业")),
    ("白酒食品", ("白酒", "非白酒", "饮料乳品", "休闲食品", "调味发酵品", "食品加工")),
    ("农业", ("农产品加", "养殖业", "种植业", "渔业", "动物保健", "饲料", "农化制品")),
    ("零售消费", ("一般零售", "专业连锁", "互联网电商", "贸易", "旅游零售", "酒店餐饮",
                  "旅游景区", "教育", "美容护理", "专业服务")),
    ("轻工纺服", ("家居用品", "造纸", "包装印刷", "文娱用品", "纺织制造", "服装家纺", "饰品")),
    ("地产基建", ("房地产开发", "房地产服务", "基础建设", "专业工程", "房屋建设",
                  "装修装饰", "装修建材", "水泥", "玻璃玻纤")),
    ("钢铁", ("普钢", "特钢", "钢铁")),
    ("有色", ("工业金属", "小金属", "贵金属", "能源金属", "金属新材料")),
    ("煤炭石油", ("煤炭开采", "焦炭", "油气开采", "炼化及贸易", "油服工程")),
    ("化工", ("化学原料", "化学制品", "化纤", "橡胶", "塑料", "非金属材料")),
    ("电力公用", ("电力", "燃气", "环境治理", "水务", "环保设备")),
    ("交运", ("航运港口", "航空机场", "物流", "铁路公路")),
    ("金融", ("银行", "证券", "保险", "多元金融")),
    ("汽车", ("汽车零部", "乘用车", "商用车", "汽车服务", "摩托车")),
    ("机械", ("通用设备", "专用设备", "自动化设", "工程机械", "轨交设备")),
    ("军工", ("航天装备", "航空装备", "地面兵装", "航海装备", "军工电子")),
    ("新能源", ("电池", "光伏设备", "风电设备", "电网设备", "其他电源设备", "电机")),
    ("家电", ("白色家电", "黑色家电", "小家电", "厨卫电器", "照明设备", "家电零部")),
)

# 新浪板块的行业是**另一套口径**：只有 48 个粗行业（"电子信息""酿酒行业"），
# 与东财二级行业名毫无交集，走 SECTOR_GROUPS 的前缀匹配会全部落空 → 每个粗行业
# 都变成独立题材，资金地图里出现「电子信息」而主线卡片写「电子」，对不上。
# 所以这里做一张**精确映射表**，只在 _sector_group 里兜底用（不参与前缀匹配）。
SINA_INDUSTRY_GROUPS: Dict[str, str] = {
    "电子信息": "电子", "电子器件": "电子", "仪器仪表": "机械", "机械行业": "机械",
    "纺织机械": "机械", "金融行业": "金融", "酿酒行业": "白酒食品", "食品行业": "白酒食品",
    "汽车制造": "汽车", "摩托车": "汽车", "传媒娱乐": "传媒", "化工行业": "化工",
    "化纤行业": "化工", "塑料制品": "化工", "生物制药": "医药", "医疗器械": "医药",
    "商业百货": "零售消费", "酒店旅游": "零售消费", "物资外贸": "零售消费",
    "电器行业": "家电", "家电行业": "家电", "有色金属": "有色", "农林牧渔": "农业",
    "农药化肥": "农业", "发电设备": "新能源", "交通运输": "交运", "建筑建材": "地产基建",
    "房地产": "地产基建", "玻璃行业": "地产基建", "水泥行业": "地产基建",
    "公路桥梁": "地产基建", "开发区": "地产基建", "陶瓷行业": "地产基建",
    "服装鞋类": "轻工纺服", "印刷包装": "轻工纺服", "纺织行业": "轻工纺服",
    "造纸行业": "轻工纺服", "家具行业": "轻工纺服", "电力行业": "电力公用",
    "环保行业": "电力公用", "供水供气": "电力公用", "钢铁行业": "钢铁",
    "煤炭行业": "煤炭石油", "石油行业": "煤炭石油", "飞机制造": "军工",
    "船舶制造": "军工", "其它行业": "其他", "综合行业": "其他",
}

# 新浪概念榜里有一批「全市场标签」：融资融券、基金重仓、股权激励、业绩预升……
# 它们的净流入常年排在最前（覆盖全市场，天然吸金），但跟「今日什么在涨」无关。
# 不滤掉的话概念榜前六名会被这类标签占满，资金地图等于废掉。
SINA_CONCEPT_NOISE = frozenset({
    # 持仓结构 / 股东背景
    "基金重仓", "保险重仓", "QFII重仓", "券商重仓", "社保重仓", "信托重仓",
    "外资背景", "高校背景", "央企50", "超大盘", "含H股", "含B股", "含GDR",
    # 交易属性 / 资本运作
    "融资融券", "股权激励", "参股金融", "金融参股", "重组概念", "业绩预升",
    "业绩预降", "分拆上市", "整体上市", "资产注入", "准ST股", "ST板块",
    "未股改", "摘帽概念", "送转潜力", "本月解禁", "三板精选", "出口退税",
    "股期概念", "科创50", "内贸规划",
})

# 新闻标题里的题材命中：分组名之外的常见同义词（标题很少直接写"传媒"，
# 而是写"短剧""AI 漫剧""影视"）。命中任一即计入该题材的新闻篇数。
THEME_ALIASES: Dict[str, Tuple[str, ...]] = {
    "传媒": ("短剧", "漫剧", "影视", "院线", "游戏", "出版", "广告", "IP", "内容", "文娱"),
    "半导体": ("半导体", "芯片", "晶圆", "光刻", "存储", "封测", "制程", "算力芯片"),
    "电子": ("PCB", "面板", "消费电子", "元器件", "被动元件", "MLCC"),
    "计算机": ("软件", "信创", "数据要素", "AI应用", "智能体", "大模型", "算力"),
    "通信": ("光模块", "CPO", "6G", "运营商", "光通信"),
    "医药": ("创新药", "CRO", "CXO", "医疗器械", "中药", "疫苗", "减肥药", "医保"),
    "白酒食品": ("白酒", "食品", "乳业", "调味品", "零食"),
    "农业": ("种业", "生猪", "养殖", "粮食", "化肥", "农药"),
    "零售消费": ("消费", "零售", "免税", "旅游", "酒店", "电商", "首发经济"),
    "地产基建": ("地产", "楼市", "基建", "水泥", "城中村"),
    "有色": ("黄金", "铜", "铝", "稀土", "锂", "白银", "钨"),
    "煤炭石油": ("煤炭", "原油", "石油", "天然气", "油价"),
    "化工": ("化工", "化纤", "农药", "涨价"),
    "电力公用": ("电力", "核电", "水电", "绿电", "环保"),
    "交运": ("航运", "港口", "航空", "物流", "运价"),
    "金融": ("券商", "银行", "保险", "资本市场", "回购增持", "再贷款"),
    "汽车": ("汽车", "整车", "零部件", "智驾", "智能驾驶"),
    "机械": ("机器人", "人形机器人", "机床", "工程机械", "减速器"),
    "军工": ("军工", "商业航天", "卫星", "无人机", "大飞机"),
    "新能源": ("光伏", "储能", "锂电", "风电", "电网", "固态电池"),
    "家电": ("家电", "白电", "以旧换新"),
}

# 「影响面大」的新闻关键词：时间轴只留这些，避免把 300 条快讯全铺上去
BIG_NEWS_WORDS = (
    "央行", "证监会", "国务院", "财政部", "发改委", "工信部", "监管", "政策", "会议",
    "降准", "降息", "关税", "进出口", "CPI", "PPI", "PMI", "GDP", "社融", "汇率",
    "美联储", "非农", "议息", "重磅", "出台", "印发", "部署", "签约", "投产",
    "业绩", "财报", "涨价", "突破", "中标", "并购", "重组", "上市",
)


# ================================================================ 抓取

def _pool(kind: str, date_str: str) -> Dict[str, Any]:
    """抓一个股票池（涨停 / 跌停 / 炸板）。

    返回统一结构：{"total", "qdate", "items"}。
    ⚠ ``qdate`` 是**服务端认定的交易日**：周末/节假日手动刷新时请求 2026-11-01
    也会返回上一个交易日的数据，页面日期必须以它为准，否则会出现
    「标题写着周六，数据其实是周五」。
    """
    url, what = {
        "zt": (EM_ZT_URL, "涨停池"),
        "dt": (EM_DT_URL, "跌停池"),
        "zb": (EM_ZB_URL, "炸板池"),
    }[kind]
    # ⚠ sort 必须传原始值 "fbt:asc"，交给 requests 去编码。
    # 手写成 "fbt%3Aasc" 会被二次编码成 fbt%253Aasc，服务端解析失败后**仍返回 total
    # 但 pool 为空** —— 看起来像「今天没有涨停股」，是最难查的一类假成功。
    sort = "fund:asc" if kind == "dt" else "fbt:asc"
    data = _fetch_json(url, {
        "ut": EM_UT, "dpt": "wz.ztzt", "Pageindex": "0", "pagesize": "300",
        "sort": sort, "date": date_str,
    }, what=what)
    body = data.get("data") or {}
    return {
        "total": int(body.get("tc") or 0),
        "qdate": str(body.get("qdate") or ""),
        "items": body.get("pool") or [],
    }


def _clist(params: Dict[str, Any], what: str) -> Dict[str, Any]:
    """打 ``clist/get``，按域名依次降级。

    只在域名之间快速切换（每个域名最多试 2 次），不在单个域名上长时间重试 ——
    风控是按路径触发的，同一个域名重试 4 次也是白等，换节点才有意义。
    全部失败才抛 HotspotError，由调用方记进 ``data_gaps``（页面会明确提示缺了哪块）。
    """
    errors: List[str] = []
    for idx, host in enumerate(EM_CLIST_HOSTS):
        try:
            return _fetch_json(host + "/api/qt/clist/get", params, what=what,
                               retries=2 if idx == len(EM_CLIST_HOSTS) - 1 else 1)
        except HotspotError as exc:
            errors.append(str(exc))
        time.sleep(_REQ_GAP * 0.5)
    raise HotspotError(f"{what} 抓取失败（全部域名不可用）：{errors[-1] if errors else '未知'}")


def _board_flow(kind: str) -> List[Dict[str, Any]]:
    """行业 / 概念板块的主力资金流（按净额降序，全量约 90 / 500 条）。

    东财这个接口单次最多返回 100 条，行业板块正好一页够；概念板块只取前 100
    （按净流入排序，头部就是当天真正有钱进的方向，尾部对「热点」没有意义）。
    """
    fs = "m:90+t:2" if kind == "industry" else "m:90+t:3"
    data = _clist({
        "fid": "f62", "po": "1", "pz": "100", "pn": "1", "np": "1",
        "fltt": "2", "invt": "2", "fs": fs,
        "fields": "f12,f14,f3,f62,f184",
    }, what=f"{'行业' if kind == 'industry' else '概念'}板块资金流")
    diff = ((data.get("data") or {}).get("diff")) or []
    out = []
    for row in diff:
        name = row.get("f14")
        if not name:
            continue
        if kind != "industry" and name.strip() in SINA_CONCEPT_NOISE:
            continue
        out.append({
            "code": row.get("f12"),
            "name": name,
            "change_pct": _f(row.get("f3")),
            "net_inflow": _f(row.get("f62")),      # 元
            "net_ratio": _f(row.get("f184")),      # 主力净占比 %
            "group": _sector_group(name) if kind == "industry" else "",
        })
    return out


def _stock_flow(direction: str, size: int = 15) -> List[Dict[str, Any]]:
    """个股主力资金流。direction=in 取净流入前列，out 取净流出前列。"""
    data = _clist({
        "fid": "f62", "po": "1" if direction == "in" else "0",
        "pz": str(size), "pn": "1", "np": "1", "fltt": "2", "invt": "2",
        "fs": FS_ALL_A, "fields": "f12,f14,f2,f3,f62",
    }, what=f"个股资金{'流入' if direction == 'in' else '流出'}榜")
    diff = ((data.get("data") or {}).get("diff")) or []
    return [{
        "code": r.get("f12"),
        "name": r.get("f14"),
        "price": _f(r.get("f2")),
        "change_pct": _f(r.get("f3")),
        "net_inflow": _f(r.get("f62")),
    } for r in diff if r.get("f14")]


# ---------------------------------------------------------------- 备用源：新浪
#
# 为什么要备源（2026-11-02 实测）：东财 push2 的 ``clist/get`` 在云环境里被
# **路径级拦截** —— 同一个域名下的 ``ulist.np/get`` 正常，push2ex 的涨停池也正常，
# 唯独 clist 一律 RemoteDisconnected，本机与线上沙箱同时如此。结果就是
# 「涨停池抓得到、资金流全空」，页面残一半。
#
# 新浪的 MoneyFlow 接口免 cookie、无签名，三套排行正好对上：
#   ssl_bkzj_bk   板块（fenlei=0 行业 / 1 概念 / 2 证监会行业 / 3 指数）
#   ssl_bkzj_ssggzj 个股
#
# ⚠ **口径差异必须在页面上写明**：新浪板块的 netamount 是「全部成交净额」
# （流入-流出），东财的 f62 是「主力净额」（超大单+大单），后者数值通常只有前者
# 的三分之一左右。两个源的绝对值不可直接比较，所以只要东财能取到就绝不混用，
# 整份日报的资金块**只来自一个源**，并把源名写进 payload.fund_source。
SINA_BK_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
               "json_v2.php/MoneyFlow.ssl_bkzj_bk")
SINA_STOCK_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
                  "json_v2.php/MoneyFlow.ssl_bkzj_ssggzj")
SINA_REFERER = "https://finance.sina.com.cn/"


def _is_fund_symbol(code: str, name: str) -> bool:
    """新浪个股榜里混着 ETF / LOF：实测前排有「中证1000ETF南方 +19.5亿」
    「短融ETF海富通 -58.4亿」，全是场内基金的资金申赎，不是个股炒作。
    不滤掉的话资金榜第一名永远是大宽基 ETF，个股榜就没意义了。"""
    code = (code or "").strip()
    if code[:2] in ("51", "52", "56", "58", "15", "16", "17", "18"):
        return True
    upper = (name or "").upper()
    return "ETF" in upper or "LOF" in upper or "基金" in (name or "")


def _board_flow_sina(kind: str) -> List[Dict[str, Any]]:
    """新浪板块资金流（行业 fenlei=0 / 概念 fenlei=1）。"""
    rows = _fetch_json(SINA_BK_URL, {
        "page": "1", "num": "200", "sort": "netamount", "asc": "0",
        "fenlei": "0" if kind == "industry" else "1",
    }, what=f"新浪{'行业' if kind == 'industry' else '概念'}板块资金流",
        expect_list=True, retries=2, referer=SINA_REFERER)

    out: List[Dict[str, Any]] = []
    for row in rows or []:
        name = row.get("name")
        if not name:
            continue
        if kind != "industry" and name.strip() in SINA_CONCEPT_NOISE:
            continue
        change = _f(row.get("avg_changeratio"))
        ratio = _f(row.get("ratioamount"))
        out.append({
            "code": row.get("category"),
            "name": name,
            # 新浪的涨跌幅/占比是小数（0.0384 = 3.84%），统一换成百分数
            "change_pct": round(change * 100, 2) if change is not None else None,
            "net_inflow": _f(row.get("netamount")),
            "net_ratio": round(ratio * 100, 2) if ratio is not None else None,
            "lead_stock": row.get("ts_name"),
            # 粗行业 → 大方向，让资金地图的行业名与主线卡片能对上（东财源下为空）
            "group": _sector_group(name) if kind == "industry" else "",
        })
    return out


def _stock_flow_sina(direction: str, size: int = 15) -> List[Dict[str, Any]]:
    """新浪个股资金流。主力净额取「超大单 + 大单」（r0_net + r1_net），
    与东财「主力净额」的定义一致，否则换成全成交净额会让数字大一圈。"""
    # 多取一些再过滤：ETF 会占掉前排，只取 size 条会被基金吃掉一半名额
    rows = _fetch_json(SINA_STOCK_URL, {
        "page": "1", "num": str(max(size * 4, 60)),
        "sort": "netamount", "asc": "0" if direction == "in" else "1",
        "bankuai": "",
    }, what=f"新浪个股资金{'流入' if direction == 'in' else '流出'}榜",
        expect_list=True, retries=2, referer=SINA_REFERER)

    packed: List[Dict[str, Any]] = []
    for row in rows or []:
        name = row.get("name")
        if not name:
            continue
        code = (row.get("symbol") or "")[-6:]
        if _is_fund_symbol(code, name):
            continue
        r0 = _f(row.get("r0_net"))
        r1 = _f(row.get("r1_net"))
        net = (r0 or 0) + (r1 or 0) if (r0 is not None or r1 is not None) \
            else _f(row.get("netamount"))
        change = _f(row.get("changeratio"))
        packed.append({
            "code": code,
            "name": name,
            "price": _f(row.get("trade")),
            "change_pct": round(change * 100, 2) if change is not None else None,
            "net_inflow": net,
        })
    packed.sort(key=lambda r: (r["net_inflow"] or 0), reverse=(direction == "in"))
    return packed[:size]


def _f(value) -> Optional[float]:
    """缺值统一转成 None。东财给 '-'，新浪给字符串数字。"""
    if value in (None, "-", "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _get(row, key: str, default=None):
    """取行字段，兼容云库 Row（row["x"]）与 SQLite ORM 实例（row.x）。

    本模块要同时跑在线上（云库）和本地（SQLite），两边的行对象接口不同：
    直接写 row["title"] 在本地会抛 ``'News' object is not subscriptable``。
    """
    if row is None:
        return default
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        return getattr(row, key, default)


# ================================================================ 归并工具

def _clean_sector(name: str) -> str:
    """去掉东财行业名里的罗马数字后缀："游戏Ⅱ" → "游戏"。"""
    return (name or "").replace("Ⅱ", "").replace("Ⅲ", "").strip()


def _sector_group(sector: str) -> str:
    """细分行业 → 大方向。未命中的原样返回（作为独立题材，靠家数门槛过滤）。

    两套行业口径先分开处理：新浪的 48 个粗行业走精确表（SINA_INDUSTRY_GROUPS），
    东财二级行业走前缀表（SECTOR_GROUPS）。混在一起匹配会互相污染。
    """
    sec = _clean_sector(sector)
    if not sec:
        return "其他"
    if sec in SINA_INDUSTRY_GROUPS:
        return SINA_INDUSTRY_GROUPS[sec]
    for group, keys in SECTOR_GROUPS:
        for key in keys:
            # 双向 startswith：涨停池的行业名被截断成 4 字，无法做等值匹配
            if sec.startswith(key) or key.startswith(sec):
                return group
    return sec


def _theme_aliases(group: str) -> Tuple[str, ...]:
    return THEME_ALIASES.get(group, (group,))


def _match_news_count(group: str, titles: Iterable[str]) -> int:
    """标题命中该题材别名的篇数。一篇新闻可同时计入多个题材（这是特性不是 bug：
    一条"AI 应用带动传媒涨停"的新闻，对传媒和计算机都是有效催化）。"""
    words = _theme_aliases(group)
    n = 0
    for title in titles:
        if any(w in title for w in words):
            n += 1
    return n


def _fmt_time(hmmss) -> str:
    """涨停池的 fbt/lbt 是 HMMSS 整数（92500 = 09:25:00）。"""
    try:
        text = str(int(hmmss)).zfill(6)
        return f"{text[0:2]}:{text[2:4]}"
    except (TypeError, ValueError):
        return ""


def _yi(value: Optional[float], digits: int = 1) -> Optional[float]:
    """元 → 亿元。"""
    if value is None:
        return None
    return round(value / 1e8, digits)


# ================================================================ 聚合

def _build_tone(db, market_date: str, zt: Dict[str, Any], dt: Dict[str, Any],
                zb: Dict[str, Any], prev_zt_total: Optional[int],
                with_db: bool = True) -> Dict[str, Any]:
    """A 区块：今日定调。

    拆成两层：
      · 池子口径（涨停/跌停/炸板/连板/封板率/晋级率）—— 只依赖抓取结果，永远有值
      · 库内口径（指数收盘、成交额、赚钱效应、温度）—— 依赖 stock_temp / a_share 当日
        是否跑过，缺了就显示「—」，绝不拿昨天的数字冒充今天
    """
    index_rows: List[Dict[str, Any]] = []
    if with_db:
        for symbol, label in (("SH000001", "上证指数"), ("SH000688", "科创50")):
            try:
                history = crud.get_market_history(db, symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning("读 %s 行情失败: %s", symbol, exc)
                history = []
            if not history:
                continue
            last = history[-1]
            prev = history[-2] if len(history) > 1 else None
            value = _f(_get(last, "value"))
            prev_value = None
            if prev is not None:
                prev_value = _f(_get(prev, "value"))
            change_pct = None
            if value is not None and prev_value:
                change_pct = round((value / prev_value - 1) * 100, 2)
            index_rows.append({
                "symbol": symbol, "name": label,
                "value": round(value, 2) if value is not None else None,
                "change_pct": change_pct,
                "date": _get(last, "date"),
            })

    indicators: Dict[str, Any] = {}
    if with_db:
        try:
            indicators = crud.get_stock_temp_indicators(db) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("读股市热度指标失败: %s", exc)

    turnover = _reading(indicators, "turnover_amt")
    breadth = _reading(indicators, "breadth")
    temperature = _reading(indicators, "temperature")

    turnover_delta = None
    if with_db and turnover is not None:
        try:
            series = crud.get_stock_temp_history_series(db, ["turnover_amt"], per_key_limit=3)
            points = series.get("turnover_amt") or []
            if len(points) >= 2:
                prev_value = _f(points[-2].get("value"))
                if prev_value:
                    turnover_delta = round(turnover - prev_value, 0)
        except Exception:  # noqa: BLE001 —— 拿不到前值就不显示增减
            turnover_delta = None

    limit_up = int(zt.get("total") or 0)
    limit_down = int(dt.get("total") or 0)
    broken = int(zb.get("total") or 0)
    seal_rate = round(limit_up / (limit_up + broken) * 100, 1) if (limit_up + broken) else None

    max_board = 0
    for item in zt.get("items") or []:
        max_board = max(max_board, int(item.get("lbc") or 1))

    promotion = None
    if prev_zt_total:
        boards = sum(1 for it in (zt.get("items") or []) if int(it.get("lbc") or 1) >= 2)
        promotion = round(boards / prev_zt_total * 100, 1)

    return {
        "index": index_rows,
        "turnover": {"value": turnover, "delta": turnover_delta},
        "breadth": breadth,
        "temperature": temperature,
        "limit": {
            "up": limit_up, "down": limit_down, "broken": broken,
            "seal_rate": seal_rate, "max_board": max_board,
            "promotion": promotion,
        },
    }


def _reading(indicators: Dict[str, Any], key: str) -> Optional[float]:
    """取某项指标的当前读数。用 _get 而不是 hasattr —— 云库 Row 对任意属性名
    都会「成功」返回（内部是 dict.get），hasattr 判断在那里恒为 True。"""
    return _f(_get(indicators.get(key), "value"))


def _build_themes(zt_items: List[Dict[str, Any]], titles: List[str],
                  prev_theme_names: Dict[str, int]) -> List[Dict[str, Any]]:
    """B 区块：今日主线。

    聚簇口径：涨停股 → 细分行业 → 大方向，同方向合并后按**涨停家数**排序。
    为什么以涨停家数为第一权重：资金净流入会被权重股扭曲（一只票就能顶起一个板块），
    而涨停家数直接反映「有多少资金愿意在板上买」，是热点最硬的证据。
    """
    buckets: Dict[str, Dict[str, Any]] = {}
    for item in zt_items:
        sector = _clean_sector(item.get("hybk") or "")
        group = _sector_group(sector)
        bucket = buckets.setdefault(group, {
            "name": group, "count": 0, "sectors": {}, "stocks": [],
            "seal_fund": 0.0, "max_board": 0,
        })
        bucket["count"] += 1
        bucket["sectors"][sector] = bucket["sectors"].get(sector, 0) + 1
        board = int(item.get("lbc") or 1)
        bucket["max_board"] = max(bucket["max_board"], board)
        bucket["seal_fund"] += _f(item.get("fund")) or 0.0
        bucket["stocks"].append({
            "code": item.get("c"), "name": item.get("n"),
            "board": board,
            "days": (item.get("zttj") or {}).get("days"),
            "sector": sector,
            "change_pct": round(_f(item.get("zdp")) or 0.0, 2),
            "seal_fund_yi": _yi(_f(item.get("fund"))),
            "first_seal": _fmt_time(item.get("fbt")),
            "broken_times": int(item.get("zbc") or 0),
        })

    themes: List[Dict[str, Any]] = []
    for bucket in buckets.values():
        # 家数太低的方向（1~2 只）只是个股行为，不配叫「主线」
        if bucket["count"] < 3:
            continue
        sectors = sorted(bucket["sectors"].items(), key=lambda kv: (-kv[1], kv[0]))
        news_count = _match_news_count(bucket["name"], titles)
        prev_days = prev_theme_names.get(bucket["name"], 0)
        bucket["stocks"].sort(key=lambda s: (-s["board"], -(s["seal_fund_yi"] or 0)))
        themes.append({
            "name": bucket["name"],
            "stage": _stage(bucket["count"], prev_days),
            "days_on_list": prev_days,
            "limit_up": bucket["count"],
            "max_board": bucket["max_board"],
            "seal_fund_yi": round(bucket["seal_fund"] / 1e8, 1),
            "news_count": news_count,
            "sectors": [{"name": n, "count": c} for n, c in sectors[:3]],
            "lead_stocks": bucket["stocks"][:6],
        })

    themes.sort(key=lambda t: (-t["limit_up"], -t["max_board"], -t["news_count"]))
    if not themes:
        return []
    # 相对阈值：只有「跟第一名差不太多」的方向才算同一天的主线。
    # 今天 125 家涨停里传媒 38 家、轻工纺服 9 家 —— 后者只是零散补涨，
    # 放进主线会让人误以为有两条并行主线。25% + 绝对 3 家两个条件同时满足。
    cut = max(3.0, themes[0]["limit_up"] * 0.25)
    return [t for t in themes if t["limit_up"] >= cut][:4]


def _stage(count: int, prev_days: int) -> Optional[str]:
    """题材阶段。需要前一日日报做基线，没有基线就返回 None（页面不显示标签）。

    判定只在「今天」与「昨天」之间做，不做更长的历史 —— 题材轮动以 1~2 天为周期，
    拉长窗口反而把「昨天的旧主线」算成今天的主线。
    """
    if prev_days <= 0:
        return "启动" if count >= 5 else None
    if count >= 8:
        return "加速"
    if count >= 5:
        return "延续"
    return "分歧"


def _norm_board_key(name: str) -> str:
    """板块名归一，用于去重。

    新浪同时挂着「光伏」与「光伏概念」「风能」与「风能概念」，两个都进榜时
    看着像两条独立主线，其实是一条。去后缀再比。
    """
    n = (name or "").strip()
    for suffix in ("概念", "行业", "板块", "指数"):
        if n.endswith(suffix) and len(n) > len(suffix) + 1:
            return n[: -len(suffix)]
    return n


def _dedupe_boards(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同名板块只留净额绝对值最大的那个（流入榜留最大正数，流出榜留最大负数）。"""
    best: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = _norm_board_key(row["name"])
        cur = best.get(key)
        if cur is None or abs(row["net_inflow"] or 0) > abs(cur["net_inflow"] or 0):
            best[key] = row
    return list(best.values())


def _build_boards(industry: List[Dict[str, Any]],
                  concepts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """D 区块：资金地图。

    三类定性沿用市面复盘文章的成熟口径，只做规则判断，不做主观解读：
      明牌启动 = 价涨钱进（涨幅 > 1% 且净流入 > 0）
      暗中建仓 = 钱进价跌（净流入 > 0 且涨幅 < 0）
      撤退     = 价钱齐跌（涨幅 < 0 且净流出 > 0）
      其他     = 涨幅与资金方向不一致、量级又不够的，<反抽>（跌多了资金回补）
    """
    def tag(change: Optional[float], net: Optional[float]) -> str:
        if change is None or net is None:
            return ""
        if net > 0 and change > 1:
            return "明牌启动"
        if net > 0 and change < 0:
            return "暗中建仓"
        if net < 0 and change < 0:
            return "撤退"
        if net > 0:
            return "资金流入"
        return "反抽"

    industry = _dedupe_boards(industry)
    concepts = _dedupe_boards(concepts)

    def pack(rows: List[Dict[str, Any]], reverse: bool) -> List[Dict[str, Any]]:
        rows = [r for r in rows if r["net_inflow"] is not None]
        if not reverse:
            # 只留真的净流出的。新浪板块口径下「全行业净流入」很常见，
            # 硬取最小 6 个会给出「陶瓷行业 +0.1 亿」这种自相矛盾的流出榜。
            rows = [r for r in rows if (r["net_inflow"] or 0) < 0]
        picked = sorted(rows, key=lambda r: (r["net_inflow"] or 0), reverse=reverse)[:6]
        return [{
            "name": r["name"],
            # 大方向：新浪源下粗行业（电子信息）→ 电子，与主线卡片同名可比；
            # 东财源下二级行业（广告营销）→ 传媒，同样归位。空串表示没映射上。
            "group": r.get("group") or "",
            "change_pct": r["change_pct"],
            "net_inflow_yi": _yi(r["net_inflow"]),
            "net_ratio": r["net_ratio"],
            "tag": tag(r["change_pct"], r["net_inflow"]),
        } for r in picked]

    return {
        "inflow": pack(industry, True),
        "outflow": pack(industry, False),
        "concepts": [{
            "name": r["name"],
            "change_pct": r["change_pct"],
            "net_inflow_yi": _yi(r["net_inflow"]),
            "tag": tag(r["change_pct"], r["net_inflow"]),
        } for r in sorted(concepts, key=lambda r: (r["net_inflow"] or 0), reverse=True)[:6]],
    }


def _build_stocks(inflow: List[Dict[str, Any]], outflow: List[Dict[str, Any]],
                  zt_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """C 区块：热股三榜。

    涨停梯队按连板高度分组（首板 / 2 板 / 3 板 …），只保留 ≥2 板的明细，
    首板家数用计数呈现 —— 首板动辄上百只，全铺出来只是噪音。
    """
    ladder: Dict[int, List[Dict[str, Any]]] = {}
    for item in zt_items:
        board = int(item.get("lbc") or 1)
        if board < 2:
            continue
        ladder.setdefault(board, []).append({
            "code": item.get("c"), "name": item.get("n"),
            "sector": _clean_sector(item.get("hybk") or ""),
            "days": (item.get("zttj") or {}).get("days"),
            "seal_fund_yi": _yi(_f(item.get("fund"))),
        })
    ladder_rows = []
    for board in sorted(ladder, reverse=True):
        items = sorted(ladder[board], key=lambda s: -(s["seal_fund_yi"] or 0))
        ladder_rows.append({"board": board, "count": len(items), "items": items[:8]})

    def pack(rows):
        return [{
            "code": r["code"], "name": r["name"],
            "price": r["price"], "change_pct": r["change_pct"],
            "net_inflow_yi": _yi(r["net_inflow"]),
        } for r in rows]

    return {
        "inflow": pack(inflow),
        "outflow": pack(outflow),
        "ladder": ladder_rows,
        "first_board_count": sum(1 for it in zt_items if int(it.get("lbc") or 1) == 1),
    }


def _build_timeline(news_rows: List[Any]) -> List[Dict[str, Any]]:
    """E 区块：今日大事。只留标题里带「影响面」关键词的，最多 8 条。"""
    picked: List[Dict[str, Any]] = []
    for row in news_rows:
        title = _get(row, "title") or ""
        if not title:
            continue
        hit = next((w for w in BIG_NEWS_WORDS if w in title), None)
        if not hit:
            continue
        publish = _get(row, "publish_time")
        picked.append({
            "title": title,
            "url": _get(row, "url"),
            "time": publish.strftime("%H:%M") if hasattr(publish, "strftime") else "",
            "sort": publish.timestamp() if hasattr(publish, "timestamp") else 0,
            "tag": _news_tag(title),
        })
    picked.sort(key=lambda x: -x["sort"])
    for item in picked:
        item.pop("sort", None)
    return picked[:8]


def _news_tag(title: str) -> str:
    """新闻分类标签（纯关键词，不做语义判断）。"""
    if any(w in title for w in ("央行", "证监会", "国务院", "财政部", "发改委", "监管",
                                "政策", "降准", "降息", "关税")):
        return "政策"
    if any(w in title for w in ("资金", "净流入", "ETF", "北向", "两融", "回购")):
        return "资金"
    if any(w in title for w in ("CPI", "PPI", "PMI", "GDP", "社融", "非农", "美联储")):
        return "数据"
    return "产业"


def _verdict(tone: Dict[str, Any], themes: List[Dict[str, Any]]) -> str:
    """一句话结论：量能 + 宽度 + 主线 + 高度，四个事实拼成一句，不做预测。"""
    limit = tone["limit"]
    parts: List[str] = []

    turnover = tone["turnover"]
    if turnover.get("value") is not None:
        delta = turnover.get("delta")
        if delta is None:
            parts.append(f"成交 {turnover['value']:.0f} 亿")
        else:
            word = "放量" if delta > 0 else "缩量"
            parts.append(f"{word} {abs(delta):.0f} 亿至 {turnover['value']:.0f} 亿")

    breadth = tone.get("breadth")
    if breadth is not None:
        if breadth >= 80:
            parts.append(f"赚钱效应 {breadth:.0f}%（普涨）")
        elif breadth >= 60:
            parts.append(f"赚钱效应 {breadth:.0f}%（偏强）")
        elif breadth >= 40:
            parts.append(f"赚钱效应 {breadth:.0f}%（结构分化）")
        else:
            parts.append(f"赚钱效应仅 {breadth:.0f}%（普跌）")

    if themes:
        top = themes[0]
        extra = f"、{themes[1]['name']}" if len(themes) > 1 else ""
        parts.append(f"主线在{top['name']}{extra}")

    if limit.get("max_board"):
        parts.append(f"最高 {limit['max_board']} 板")

    temperature = tone.get("temperature")
    if temperature is not None:
        if temperature >= 65:
            parts.append("情绪偏热")
        elif temperature >= 40:
            parts.append("情绪中性")
        else:
            parts.append("情绪偏冷")

    return "，".join(parts) + "。" if parts else "今日数据尚未生成。"


# ================================================================ 主入口

def build_daily_payload(db, market_date: Optional[str] = None,
                        *, keep_db_payload: bool = True) -> Dict[str, Any]:
    """抓取 + 聚合，返回完整 payload（不落库）。

    Args:
        market_date: 目标交易日 YYYY-MM-DD，为空取今天。
                     非交易日会自动被涨停池的 ``qdate`` 校正回最近交易日。
        keep_db_payload: 是否读库内（新闻 / 指标 / 指数 / 昨日日报）。
                     关掉可以只测抓取链路。
    """
    target = market_date or datetime.now().strftime("%Y-%m-%d")
    compact = target.replace("-", "")
    gaps: List[str] = []
    budget_start = time.monotonic()

    def _over_budget(what: str) -> bool:
        """抓取阶段总耗时兜底（见 _BUDGET_SECONDS 的注释）。"""
        if time.monotonic() - budget_start <= _BUDGET_SECONDS:
            return False
        gaps.append(f"{what}：抓取超时预算（{_BUDGET_SECONDS}s），本次跳过")
        logger.warning("热点：%s 超出抓取预算，跳过", what)
        return True

    # ---- 1. 三个股票池（串行，池之间有间隔，别并发打同一个域名）
    pools: Dict[str, Dict[str, Any]] = {}
    for kind in ("zt", "dt", "zb"):
        if _over_budget(f"{kind} 池"):
            pools.setdefault(kind, {"total": 0, "qdate": "", "items": []})
            continue
        try:
            pools[kind] = _pool(kind, compact)
        except HotspotError as exc:
            logger.error("热点：%s", exc)
            gaps.append(str(exc))
            pools[kind] = {"total": 0, "qdate": "", "items": []}
        time.sleep(_REQ_GAP)

    # 用服务端给的交易日校正（周末/节假日刷新时，请求日期与实际交易日不同）
    real_date = target
    for pool in pools.values():
        qdate = pool.get("qdate") or ""
        if len(qdate) == 8:
            real_date = f"{qdate[0:4]}-{qdate[4:6]}-{qdate[6:8]}"
            break

    # ---- 2. 板块与个股资金流（整组同源，绝不混用两个口径 —— 见 SINA_BK_URL 注释）
    industry: List[Dict[str, Any]] = []
    concepts: List[Dict[str, Any]] = []
    stock_in: List[Dict[str, Any]] = []
    stock_out: List[Dict[str, Any]] = []
    fund_source = ""

    def _load_fund(industry_fn, concept_fn, in_fn, out_fn) -> None:
        nonlocal industry, concepts, stock_in, stock_out
        industry = industry_fn()
        time.sleep(_REQ_GAP)
        concepts = concept_fn()
        time.sleep(_REQ_GAP)
        stock_in = in_fn()
        time.sleep(_REQ_GAP)
        stock_out = out_fn()

    if not _over_budget("资金流（东财）"):
        try:
            _load_fund(lambda: _board_flow("industry"), lambda: _board_flow("concept"),
                       lambda: _stock_flow("in"), lambda: _stock_flow("out"))
            fund_source = "东方财富"
        except HotspotError as exc:
            # 只记一句：整组走同一个接口，第一个失败后面基本也失败，
            # 逐条记 gaps 会把页面提示刷成四行一样的字。
            logger.error("热点：东财资金流不可用，改用新浪 — %s", exc)
            gaps.append("东财资金流接口不可用，资金块已改用新浪财经")

    if not fund_source and not _over_budget("资金流（新浪）"):
        try:
            _load_fund(lambda: _board_flow_sina("industry"),
                       lambda: _board_flow_sina("concept"),
                       lambda: _stock_flow_sina("in"),
                       lambda: _stock_flow_sina("out"))
            fund_source = "新浪财经"
        except HotspotError as exc:
            logger.error("热点：新浪资金流也不可用 — %s", exc)
            gaps.append(str(exc))

    # ---- 3. 库内数据
    titles: List[str] = []
    news_rows: List[Any] = []
    prev_theme_names: Dict[str, int] = {}
    prev_zt_total: Optional[int] = None

    if keep_db_payload:
        try:
            news_rows = crud.get_news_list(db, skip=0, limit=300, today_only=True) or []
            titles = [_get(r, "title") or "" for r in news_rows]
        except Exception as exc:  # noqa: BLE001
            logger.warning("热点：读当日新闻失败 %s", exc)
            gaps.append("当日新闻读取失败")

        # 昨日涨停家数：晋级率的分母。取行情表最近两个交易日，避开「自己算节假日」
        try:
            history = crud.get_market_history(db, "SH000001") or []
            if len(history) >= 2:
                prev_day = _get(history[-2], "date")
                if prev_day and not _over_budget("昨日涨停池"):
                    prev_pool = _pool("zt", str(prev_day).replace("-", ""))
                    prev_zt_total = int(prev_pool.get("total") or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("热点：读昨日涨停家数失败 %s", exc)

        # 昨日日报的主线（用于「连续 N 日上榜」与阶段判定）
        try:
            dates = crud.list_hotspot_dates(db, limit=2)
            yesterday = [d for d in dates if d < real_date]
            if yesterday:
                prev = crud.get_daily_hotspot(db, yesterday[0])
                for theme in ((prev or {}).get("payload") or {}).get("themes") or []:
                    prev_theme_names[theme.get("name")] = int(theme.get("days_on_list") or 0) + 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("热点：读昨日日报失败 %s", exc)

    # ---- 4. 聚合
    tone = _build_tone(db, real_date, pools["zt"], pools["dt"], pools["zb"],
                       prev_zt_total, with_db=keep_db_payload)
    themes = _build_themes(pools["zt"]["items"], titles, prev_theme_names)
    boards = _build_boards(industry, concepts)
    stocks = _build_stocks(stock_in, stock_out, pools["zt"]["items"])
    timeline = _build_timeline(news_rows)

    payload: Dict[str, Any] = {
        "market_date": real_date,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "tone": tone,
        "verdict": _verdict(tone, themes),
        "themes": themes,
        "boards": boards,
        "stocks": stocks,
        "timeline": timeline,
        # 资金块的实际来源：页面要显示它 —— 东财是「主力净额」，新浪板块是
        # 「全部成交净额」，两者绝对值不可比，不标出来会让人误以为是同一口径。
        "fund_source": fund_source,
        "data_gaps": gaps,
    }
    payload["item_count"] = (
        int(pools["zt"].get("total") or 0) + len(themes) + len(boards["inflow"])
        + len(stocks["inflow"])
    )
    return payload


def refresh_hotspot(db, market_date: Optional[str] = None) -> Dict[str, Any]:
    """抓取 + 聚合 + 落库，返回落库结果摘要（供 scheduler / API 打日志）。"""
    payload = build_daily_payload(db, market_date)
    real_date = payload["market_date"]
    # source 写实际用的资金源，事后排查「这批数字是哪个口径」时不用猜
    fund_src = payload.get("fund_source") or ""
    source_tag = "eastmoney:zt_pool" + (
        "+em_fund" if fund_src == "东方财富" else "+sina_fund" if fund_src else "")
    ok = crud.save_daily_hotspot(
        db, real_date, payload,
        source=source_tag,
        item_count=int(payload.get("item_count") or 0),
    )

    # 昨夜刚上线时用「今天」生成的日报，日期可能是上一个交易日（周末刷新）：
    # 这时读回来的最新一份就是它，不用额外处理。
    board_in = len(payload["boards"]["inflow"])
    summary = {
        "market_date": real_date,
        "limit_up": payload["tone"]["limit"]["up"],
        "limit_down": payload["tone"]["limit"]["down"],
        "themes": [t["name"] for t in payload["themes"]],
        "boards": board_in,
        "fund_source": payload.get("fund_source") or "无",
        "gaps": payload["data_gaps"],
        "saved": ok,
    }
    logger.info("今日热点已生成 %s：%s", real_date, json.dumps(summary, ensure_ascii=False))
    return summary
