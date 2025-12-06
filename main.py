import os
import time
import logging
from collections import defaultdict, deque
from datetime import datetime

import requests
from dotenv import load_dotenv
from io import BytesIO
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ================== 基本配置 ==================

load_dotenv("profile.env")  # 默认读取当前目录的 .env 文件

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

PRICE_CHANGE_THRESHOLD = float(os.getenv("PRICE_CHANGE_THRESHOLD", "0.03"))  # 3%
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
WINDOW_MINUTES = int(os.getenv("WINDOW_MINUTES", "15"))

# 新增：同一个币种+方向的最小提醒间隔（秒），默认 60 秒
ALERT_MIN_INTERVAL_SECONDS = int(os.getenv("ALERT_MIN_INTERVAL_SECONDS", "60"))
# 新增：记录 alert key 的最后提醒时间（key = "BASE:UP" / "BASE:DOWN"）
last_alert_key_time = {}

# 30 分钟无消息则重置次数（可在 .env 里改 ALERT_RESET_SECONDS）
ALERT_RESET_SECONDS = int(os.getenv("ALERT_RESET_SECONDS", "1800"))

# 记录每个 base 币种的涨/跌告警次数与时间
# 结构：base -> {"last_dir": "UP"/"DOWN"/None,
#                "up_count": int, "down_count": int,
#                "last_up_ts": float, "last_down_ts": float}
alert_streak_state = {}

# 新增：每个 (base + 方向) 最后一条告警的 message_id，用来 reply
# key = f"{base_asset}:UP" / f"{base_asset}:DOWN"
alert_last_message_id = {}

# 新增：配置需要屏蔽的币种（base asset），默认屏蔽 BTTC
# 例子：BLACKLIST_BASES=BTTC,PEPE,1000BONK
BLACKLIST_BASES = os.getenv("BLACKLIST_BASES", "BTTC")
BLOCKED_BASES = {b.strip().upper() for b in BLACKLIST_BASES.split(",") if b.strip()}

# Binance 端点
BINANCE_SPOT_BASE = "https://api.binance.com"
BINANCE_FAPI_BASE = "https://fapi.binance.com"  # U 本位
BINANCE_DAPI_BASE = "https://dapi.binance.com"  # 币本位

# OI 变化统计窗口（分钟）
OI_WINDOW_MINUTES = int(os.getenv("OI_WINDOW_MINUTES", "15"))

# 映射分钟 -> Binance period
OI_PERIOD_MAP = {
    5: "5m",
    15: "15m",
    30: "30m",
    60: "1h",
    120: "2h",
    240: "4h",
    360: "6h",
    720: "12h",
    1440: "1d",
}


def _get_oi_period_and_label(window_minutes: int):
    """把分钟数映射成 Binance period 和展示用的 label"""
    if window_minutes in OI_PERIOD_MAP:
        actual_minutes = window_minutes
        period = OI_PERIOD_MAP[window_minutes]
    else:
        # 不在表里的就找一个最近的
        closest = min(OI_PERIOD_MAP.keys(), key=lambda k: abs(k - window_minutes))
        actual_minutes = closest
        period = OI_PERIOD_MAP[closest]

    if actual_minutes < 60:
        label = f"{actual_minutes} min"
    elif actual_minutes == 1440:
        label = "1 d"
    elif actual_minutes % 60 == 0:
        label = f"{actual_minutes // 60} h"
    else:
        label = f"{actual_minutes} min"

    return period, label, actual_minutes


# 全局：OI_PERIOD 给 Binance API 用，OI_WINDOW_LABEL 用来显示
OI_PERIOD, OI_WINDOW_LABEL, OI_WINDOW_MINUTES = _get_oi_period_and_label(OI_WINDOW_MINUTES)


# 价格历史 & 最后提醒时间（只保留 U 本位合约）
price_history = {
    "um": defaultdict(lambda: deque()),
}
last_alert_time = {
    "um": {},
}

# CoinGecko 市值缓存：symbol -> {mc, fdv}
coingecko_cache = {}
# CoinGecko 最后更新时间（用于定期刷新）
last_coingecko_update = 0
# CoinGecko 刷新间隔（秒），默认 6 小时
COINGECKO_REFRESH_INTERVAL = int(os.getenv("COINGECKO_REFRESH_INTERVAL", "21600"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# ================== 工具函数 ==================

def send_telegram_message(text: str, reply_to_message_id=None):
    """发送 Telegram 文本消息，返回 message_id 或 None"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("未设置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，无法发送 Telegram。")
        return None

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": False,
    }
    if reply_to_message_id is not None:
        data["reply_to_message_id"] = reply_to_message_id
        data["allow_sending_without_reply"] = True

    try:
        resp = requests.post(url, data=data, timeout=10)
        if not resp.ok:
            logging.warning("发送 Telegram 失败: %s", resp.text)
            return None
        try:
            js = resp.json()
            return js.get("result", {}).get("message_id")
        except Exception:
            return None
    except Exception as e:
        logging.exception("发送 Telegram 异常: %s", e)
        return None


def send_telegram_photo(photo_bytes, caption=None, reply_to_message_id=None):
    """发送 Telegram 图片（PNG 二进制），返回 message_id 或 None"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("未设置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，无法发送 Telegram 图片。")
        return None

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", photo_bytes)}
    data = {"chat_id": TELEGRAM_CHAT_ID}
    if caption:
        data["caption"] = caption
    if reply_to_message_id is not None:
        data["reply_to_message_id"] = reply_to_message_id
        data["allow_sending_without_reply"] = True

    try:
        resp = requests.post(url, data=data, files=files, timeout=20)
        if not resp.ok:
            logging.warning("发送 Telegram 图片失败: %s", resp.text)
            return None
        try:
            js = resp.json()
            return js.get("result", {}).get("message_id")
        except Exception:
            return None
    except Exception as e:
        logging.exception("发送 Telegram 图片异常: %s", e)
        return None



def human_readable_number(x):
    """数字缩写：2800000000 -> 2.8B"""
    try:
        x = float(x)
    except Exception:
        return "N/A"
    abs_x = abs(x)
    if abs_x >= 1e12:
        return f"{x/1e12:.1f}T"
    if abs_x >= 1e9:
        return f"{x/1e9:.1f}B"
    if abs_x >= 1e6:
        return f"{x/1e6:.1f}M"
    if abs_x >= 1e3:
        return f"{x/1e3:.1f}K"
    return f"{x:.2f}"


def extract_base_asset(binance_symbol: str) -> str:
    """
    从币安 symbol 提取 base asset

    例如:
    - BTCUSDT      -> BTC
    - ETHFDUSD     -> ETH
    - BTCUSD_PERP  -> BTC
    """
    base = binance_symbol
    # 先去掉 *_PERP 后缀（币本位永续）
    if base.endswith("_PERP"):
        base = base[:-5]
    # 再去掉常见 quote 货币后缀
    for quote in ["USDT", "BUSD", "FDUSD", "USDC", "BTC", "USD"]:
        if base.endswith(quote):
            base = base[: -len(quote)]
            break
    return base.upper()


def load_coingecko_marketcaps():
    """从 CoinGecko 拉一份 symbol -> (mc, fdv) 映射"""
    global coingecko_cache, last_coingecko_update
    logging.info("从 CoinGecko 拉取市场数据（用于 MC / FDV）...")
    cache = {}
    page = 1
    while True:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 250,
            "page": page,
            "sparkline": "false",
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logging.warning("获取 CoinGecko 数据失败 (page %d): %s", page, e)
            break

        if not data:
            break

        for coin in data:
            symbol = str(coin.get("symbol", "")).upper()
            mc = coin.get("market_cap")
            fdv = coin.get("fully_diluted_valuation")
            # 如果有重复 symbol，保留市值更大的
            if symbol not in cache or (mc or 0) > (cache[symbol]["mc"] or 0):
                cache[symbol] = {"mc": mc, "fdv": fdv}
        page += 1
        # 限制页数，防止太多请求
        if page > 10:
            break

    coingecko_cache = cache
    last_coingecko_update = time.time()
    logging.info("CoinGecko 市值缓存完成，共 %d 个 symbol", len(coingecko_cache))


def get_mc_fdv_from_symbol(binance_symbol: str):
    """
    返回 (MC 字符串, FDV 字符串, MC 数值, FDV 数值)
    数值为 None 说明无法获取
    """
    base = extract_base_asset(binance_symbol)
    info = coingecko_cache.get(base)
    if not info:
        return "N/A", "N/A", None, None

    mc = info.get("mc")
    fdv = info.get("fdv")
    mc_val = float(mc) if mc is not None else None
    fdv_val = float(fdv) if fdv is not None else None

    return human_readable_number(mc), human_readable_number(fdv), mc_val, fdv_val



def build_tradingview_1m_link(binance_symbol: str, market: str) -> str:
    """
    构造 TradingView 1 分钟 K 线链接
    这里简单地用 BINANCE:<symbol>，即：
    https://www.tradingview.com/chart/?symbol=BINANCE:BTCUSDT&interval=1
    """
    tv_symbol = binance_symbol
    return f"https://www.tradingview.com/chart/?symbol=BINANCE:{tv_symbol}.P&interval=1"


# ================== Binance 数据拉取 ==================

def fetch_spot_24h_tickers():
    """现货 24h 行情（只保留 USDT，对黑名单做过滤）"""
    url = f"{BINANCE_SPOT_BASE}/api/v3/ticker/24hr"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    result = []
    for item in data:
        symbol = item["symbol"]
        if not symbol.endswith("USDT"):
            continue
        if symbol.endswith("UPUSDT") or symbol.endswith("DOWNUSDT"):
            continue
        base = extract_base_asset(symbol)
        if base in BLOCKED_BASES:
            continue
        result.append(item)
    return result


def fetch_futures_24h_tickers(market: str):
    """
    合约 24h 行情
    market: 'um' -> U 本位; 'cm' -> 币本位
    """
    if market == "um":
        base = BINANCE_FAPI_BASE
    else:
        base = BINANCE_DAPI_BASE
    url = f"{base}/fapi/v1/ticker/24hr" if market == "um" else f"{base}/dapi/v1/ticker/24hr"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    filtered = []
    for item in data:
        symbol = item["symbol"]
        base_asset = extract_base_asset(symbol)
        if base_asset in BLOCKED_BASES:
            continue
        filtered.append(item)
    return filtered


def fetch_open_interest_stats(symbol: str, market: str, retry=True):
    """
    获取当前 OI、指定窗口内 OI 变化率，以及当前 OI 的美元价值
    返回: (oi_str, oi_change_str, oi_value_usd or None)
    """
    try:
        if market == "um":
            base = BINANCE_FAPI_BASE
        else:  # "cm"
            base = BINANCE_DAPI_BASE

        hist_url = f"{base}/futures/data/openInterestHist"

        # 用全局的 OI_PERIOD 与 limit=2，大致覆盖 OI_WINDOW_MINUTES
        params = {"symbol": symbol, "period": OI_PERIOD, "limit": 2}
        hist_resp = requests.get(hist_url, params=params, timeout=10)
        hist_resp.raise_for_status()
        hist = hist_resp.json()

        if not hist:
            logging.warning("获取 %s OI 数据返回空列表", symbol)
            return "N/A", "N/A", None

        latest = hist[-1]
        current_oi_value = float(latest.get("sumOpenInterestValue", 0.0) or 0.0)

        if len(hist) >= 2:
            oldest = hist[0]
            old_oi_value = float(oldest.get("sumOpenInterestValue", 0.0) or 0.0)
            if old_oi_value > 0:
                change_pct = (current_oi_value - old_oi_value) / old_oi_value
                change_str = f"{change_pct * 100:+.2f}%"
            else:
                change_str = "N/A"
        else:
            change_str = "N/A"
        oi_display_str = "$" + human_readable_number(current_oi_value)
        return oi_display_str, change_str, current_oi_value
    except Exception as e:
        logging.warning("获取 %s OI 数据失败: %s (market: %s, period: %s)", symbol, e, market, OI_PERIOD)
        # 重试一次
        if retry:
            logging.info("重试获取 %s OI 数据...", symbol)
            time.sleep(0.5)
            return fetch_open_interest_stats(symbol, market, retry=False)
        return "N/A", "N/A", None


# fetch 1m k chart line
def fetch_1m_klines(symbol: str, market: str, limit: int = 240):
    """
    获取某个交易对最近 limit 根 1 分钟 K 线
    market: 'spot' / 'um' / 'cm'
    """
    if market == "spot":
        base = BINANCE_SPOT_BASE
        path = "/api/v3/klines"
    elif market == "um":
        base = BINANCE_FAPI_BASE
        path = "/fapi/v1/klines"
    else:  # "cm"
        base = BINANCE_DAPI_BASE
        path = "/dapi/v1/klines"

    params = {"symbol": symbol, "interval": "1m", "limit": limit}
    resp = requests.get(base + path, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()

#generate candle
def generate_1m_candlestick_png(symbol: str, market: str, limit: int = 120):
    """
    生成 1 分钟 K 线蜡烛图的 PNG 二进制，失败则返回 None
    """
    try:
        klines = fetch_1m_klines(symbol, market, limit)
        if not klines:
            return None

        # klines 每条：[open_time, open, high, low, close, volume, close_time, ...]
        times = []
        opens = []
        highs = []
        lows = []
        closes = []

        for k in klines:
            # k[0] 是毫秒时间戳，用 datetime + date2num 代替 epoch2num
            ts = datetime.fromtimestamp(k[0] / 1000.0)
            t = mdates.date2num(ts)
            times.append(t)
            opens.append(float(k[1]))
            highs.append(float(k[2]))
            lows.append(float(k[3]))
            closes.append(float(k[4]))

        fig, ax = plt.subplots(figsize=(10, 4))

        # 颜色：涨绿跌红
        up_color = "#26a69a"
        down_color = "#ef5350"

        for t, o, h, l, c in zip(times, opens, highs, lows, closes):
            color = up_color if c >= o else down_color
            # 上下影线
            ax.vlines(t, l, h, linewidth=1, color=color)
            # 实体
            ax.vlines(t, o, c, linewidth=4, color=color)

        ax.set_title(f"{symbol} - 1m")
        ax.set_ylabel("Price")
        ax.grid(True, linestyle="--", linewidth=0.3, alpha=0.5)

        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
        fig.autofmt_xdate()

        buf = BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=150)
        buf.seek(0)
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        logging.warning("生成 %s 1m K 线图失败: %s", symbol, e)
        return None

def update_alert_streak(base_asset: str, direction_flag: str, now_ts: float):
    """
    更新某个 base 币种在某个方向上的告警次数，并返回：
    (当前是第几次告警, 上一次同方向告警距今多少分钟 or None)
    direction_flag: "UP" 或 "DOWN"
    """
    state = alert_streak_state.get(base_asset, {
        "last_dir": None,
        "up_count": 0,
        "down_count": 0,
        "last_up_ts": 0.0,
        "last_down_ts": 0.0,
    })

    if direction_flag == "UP":
        prev_ts = state.get("last_up_ts", 0.0) or 0.0
        minutes_since_prev = None
        if prev_ts > 0:
            minutes_since_prev = (now_ts - prev_ts) / 60.0

        # 方向切换 或 间隔超过 ALERT_RESET_SECONDS -> 重置次数为 1
        reset_needed = (state.get("last_dir") != "UP") or (prev_ts == 0.0) or (now_ts - prev_ts > ALERT_RESET_SECONDS)
        if reset_needed:
            state["up_count"] = 1
        else:
            state["up_count"] = state.get("up_count", 0) + 1

        state["last_up_ts"] = now_ts
        state["last_dir"] = "UP"
        count = state["up_count"]
    else:  # DOWN
        prev_ts = state.get("last_down_ts", 0.0) or 0.0
        minutes_since_prev = None
        if prev_ts > 0:
            minutes_since_prev = (now_ts - prev_ts) / 60.0

        reset_needed = (state.get("last_dir") != "DOWN") or (prev_ts == 0.0) or (now_ts - prev_ts > ALERT_RESET_SECONDS)
        if reset_needed:
            state["down_count"] = 1
        else:
            state["down_count"] = state.get("down_count", 0) + 1

        state["last_down_ts"] = now_ts
        state["last_dir"] = "DOWN"
        count = state["down_count"]

    alert_streak_state[base_asset] = state
    return count, minutes_since_prev



# ================== 监控 & 告警逻辑 ==================

def update_and_check_market(market: str, tickers: list):
    """
    更新某个市场(spot/um/cm)的价格历史，并检查是否触发 15min 告警
    """
    now_ts = time.time()
    window_seconds = WINDOW_MINUTES * 60

    for item in tickers:
        symbol = item["symbol"]

        # 再保险一层黑名单过滤
        base_asset = extract_base_asset(symbol)
        if base_asset in BLOCKED_BASES:
            continue

        try:
            last_price = float(item["lastPrice"])
        except Exception:
            continue

        history = price_history[market][symbol]

        # 追加当前价格
        history.append((now_ts, last_price))

        # 去掉窗口外的数据
        while history and (now_ts - history[0][0] > window_seconds):
            history.popleft()

        if len(history) < 2:
            continue

        base_ts, base_price = history[0]
        if base_price <= 0:
            continue

        change_pct = (last_price - base_price) / base_price

        if abs(change_pct) < PRICE_CHANGE_THRESHOLD:
            continue

        # 同一个「base 币种 + 方向」在全局至少间隔 ALERT_MIN_INTERVAL_SECONDS 秒
        direction_flag = "UP" if change_pct > 0 else "DOWN"
        alert_key = f"{base_asset}:{direction_flag}"
        last_ts_key = last_alert_key_time.get(alert_key, 0)
        if now_ts - last_ts_key < ALERT_MIN_INTERVAL_SECONDS:
            continue
        last_alert_key_time[alert_key] = now_ts

        # 计算「第几次告警」以及上一次同方向告警的时间
        alert_count, minutes_since_prev = update_alert_streak(base_asset, direction_flag, now_ts)

        # 24h 涨幅 & 成交额
        try:
            chg_24h = float(item.get("priceChangePercent", 0.0))
        except Exception:
            chg_24h = 0.0
        vol_quote = item.get("quoteVolume") or item.get("volume") or "0"

        # MC / FDV（带原始数值）
        mc_str, fdv_str, mc_raw, fdv_raw = get_mc_fdv_from_symbol(symbol)

        # OI 及 OI 变化（只对合约有）
        if market in ("um", "cm"):
            oi_str, oi_change_str, oi_value_usd = fetch_open_interest_stats(symbol, market)
        else:
            oi_str, oi_change_str, oi_value_usd = "N/A", "N/A", None

        # OI / 市值 比率
        oi_mc_ratio_str = "N/A"
        if oi_value_usd is not None and oi_value_usd > 0 and mc_raw is not None and mc_raw > 0:
            try:
                ratio = oi_value_usd / mc_raw
                oi_mc_ratio_str = f"{ratio * 100:.2f}%"
            except Exception:
                oi_mc_ratio_str = "N/A"

        # 方向 & 中文文案
        direction = "📈 涨" if change_pct > 0 else "📉 跌"
        dir_cn = "上涨" if direction_flag == "UP" else "下跌"

        # 上一次同方向告警时间
        if minutes_since_prev is None:
            last_alert_text = "上一次同方向告警: 首次告警"
        else:
            last_alert_text = f"上一次同方向告警: {minutes_since_prev:.1f} 分钟前"

        # 更好看的 symbol 展示
        pretty_symbol = symbol
        if symbol.endswith("USDT"):
            pretty_symbol = symbol.replace("USDT", "/USDT")

        tradingview_link = build_tradingview_1m_link(symbol, market)

        text_lines = [
            f"{direction} [{pretty_symbol}] {change_pct * 100:+.2f}% in {WINDOW_MINUTES} min | {dir_cn}第 {alert_count} 次告警",
            f"${base_price:.4f} → ${last_price:.4f}",
            f"24h: {chg_24h:+.2f}% | Vol: ${human_readable_number(vol_quote)}",
            f"MC: {mc_str} | FDV: {fdv_str} | OI: {oi_str} | OI/MC: {oi_mc_ratio_str}",
            f"{OI_WINDOW_LABEL} 内 OI 变化: {oi_change_str}",
            last_alert_text,
            f"1m K线 (TradingView): {tradingview_link}",
        ]

        msg = "\n".join(text_lines)
        logging.info("触发告警：%s", msg.replace("\n", " | "))

        # 如果是同一方向的连续告警，则回复上一条同方向消息
        prev_msg_id = alert_last_message_id.get(alert_key)
        reply_to_id = prev_msg_id if (alert_count > 1 and prev_msg_id is not None) else None

        chart_bytes = generate_1m_candlestick_png(symbol, market, limit=240)
        if chart_bytes:
            message_id = send_telegram_photo(chart_bytes, caption=msg, reply_to_message_id=reply_to_id)
        else:
            message_id = send_telegram_message(msg, reply_to_message_id=reply_to_id)

        # 记录本次消息 id，供后续 reply 使用
        if message_id is not None:
            alert_last_message_id[alert_key] = message_id


def startup_message(um_count: int):
    """启动成功提示（推送到 TG）"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = (
        "✅ 监控系统运行成功！\n"
        f"当前时间: {now_str}\n"
        f"监控模式: 仅 U 本位合约\n"
        f"检测到 U 本位合约: {um_count} 个\n"
        f"屏蔽币种(按 base asset): {', '.join(sorted(BLOCKED_BASES)) if BLOCKED_BASES else '无'}\n"
        f"CoinGecko 缓存: {len(coingecko_cache)} 个 symbol"
    )
    logging.info(text.replace("\n", " | "))
    send_telegram_message(text)


# ================== 主循环 ==================

def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("请先在 .env 中配置 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID ！")
        # 仍然允许运行，只是不能发消息

    load_coingecko_marketcaps()

    # 先拉一次数据，统计数量并发"运行成功"提示

    um_tickers = fetch_futures_24h_tickers("um")



    startup_message(len(um_tickers))



    # 初始填充历史价格（让 15min 统计尽快生效）

    update_and_check_market("um", um_tickers)



    logging.info(

        "开始循环监控：仅 U 本位合约，窗口=%d 分钟，波动阈值=%.2f%%，循环间隔=%d 秒",

        WINDOW_MINUTES,

        PRICE_CHANGE_THRESHOLD * 100,

        CHECK_INTERVAL_SECONDS,

    )



    while True:

        loop_start = time.time()

        

        # 定期刷新 CoinGecko 缓存

        if time.time() - last_coingecko_update > COINGECKO_REFRESH_INTERVAL:

            logging.info("CoinGecko 缓存已超过 %d 秒，开始刷新...", COINGECKO_REFRESH_INTERVAL)

            try:

                load_coingecko_marketcaps()

                logging.info("CoinGecko 市值缓存刷新完成")

            except Exception as e:

                logging.warning("刷新 CoinGecko 缓存失败: %s", e)



        # 只监控 U 本位合约

        try:

            um_tickers = fetch_futures_24h_tickers("um")

            update_and_check_market("um", um_tickers)

        except Exception as e:

            logging.warning("拉取 U 本位合约数据失败: %s", e)



        elapsed = time.time() - loop_start

        sleep_time = max(5, CHECK_INTERVAL_SECONDS - elapsed)

        time.sleep(sleep_time)



if __name__ == "__main__":
    main()
