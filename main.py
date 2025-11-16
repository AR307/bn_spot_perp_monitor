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
ALERT_COOLDOWN_SECONDS = WINDOW_MINUTES * 60  # 同一币种至少间隔一个窗口再提醒

# 新增：配置需要屏蔽的币种（base asset），默认屏蔽 BTTC
# 例子：BLACKLIST_BASES=BTTC,PEPE,1000BONK
BLACKLIST_BASES = os.getenv("BLACKLIST_BASES", "BTTC")
BLOCKED_BASES = {b.strip().upper() for b in BLACKLIST_BASES.split(",") if b.strip()}

# Binance 端点
BINANCE_SPOT_BASE = "https://api.binance.com"
BINANCE_FAPI_BASE = "https://fapi.binance.com"  # U 本位
BINANCE_DAPI_BASE = "https://dapi.binance.com"  # 币本位

# 价格历史 & 最后提醒时间
price_history = {
    "spot": defaultdict(lambda: deque()),
    "um": defaultdict(lambda: deque()),
    "cm": defaultdict(lambda: deque()),
}
last_alert_time = {
    "spot": {},
    "um": {},
    "cm": {},
}

# CoinGecko 市值缓存：symbol -> {mc, fdv}
coingecko_cache = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# ================== 工具函数 ==================

def send_telegram_message(text: str) -> None:
    """发送 Telegram 文本消息"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("未设置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，无法发送 Telegram。")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                # 为了让 TradingView 链接显示预览，这里设为 False
                "disable_web_page_preview": False,
            },
            timeout=10,
        )
        if not resp.ok:
            logging.warning("发送 Telegram 失败: %s", resp.text)
    except Exception as e:
        logging.exception("发送 Telegram 异常: %s", e)

def send_telegram_photo(photo_bytes, caption=None):
    """发送 Telegram 图片（PNG 二进制）"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("未设置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，无法发送 Telegram 图片。")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", photo_bytes)}
    data = {"chat_id": TELEGRAM_CHAT_ID}
    if caption:
        data["caption"] = caption

    try:
        resp = requests.post(url, data=data, files=files, timeout=20)
        if not resp.ok:
            logging.warning("发送 Telegram 图片失败: %s", resp.text)
    except Exception as e:
        logging.exception("发送 Telegram 图片异常: %s", e)


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
    global coingecko_cache
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
            logging.warning("获取 CoinGecko 数据失败: %s", e)
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
    logging.info("CoinGecko 市值缓存完成，共 %d 个 symbol", len(coingecko_cache))


def get_mc_fdv_from_symbol(binance_symbol: str):
    """根据 base asset 从 CoinGecko 缓存中拿 MC / FDV"""
    base = extract_base_asset(binance_symbol)
    info = coingecko_cache.get(base)
    if not info:
        return "N/A", "N/A"
    return human_readable_number(info["mc"]), human_readable_number(info["fdv"])


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


def fetch_open_interest_and_change_15m(symbol: str, market: str):
    """
    获取当前 OI 和大约 15 分钟内 OI 变化百分比（只对合约）
    """
    try:
        if market == "um":
            base = BINANCE_FAPI_BASE
            open_interest_url = f"{base}/fapi/v1/openInterest"
            hist_url = f"{base}/futures/data/openInterestHist"
        else:
            base = BINANCE_DAPI_BASE
            open_interest_url = f"{base}/dapi/v1/openInterest"
            hist_url = f"{base}/futures/data/openInterestHist"

        # 当前 OI
        oi_resp = requests.get(open_interest_url, params={"symbol": symbol}, timeout=10)
        oi_resp.raise_for_status()
        current_oi = float(oi_resp.json().get("openInterest", 0.0))

        # 最近 4 根 5m 的 OI 历史（大概覆盖 15m+）
        hist_resp = requests.get(
            hist_url,
            params={"symbol": symbol, "period": "5m", "limit": 4},
            timeout=10,
        )
        hist_resp.raise_for_status()
        hist = hist_resp.json()
        if len(hist) < 2:
            return human_readable_number(current_oi), "N/A"
        old_oi = float(hist[0].get("sumOpenInterest", 0.0))
        if old_oi <= 0:
            return human_readable_number(current_oi), "N/A"
        change_pct = (current_oi - old_oi) / old_oi
        return human_readable_number(current_oi), f"{change_pct * 100:+.2f}%"
    except Exception as e:
        logging.warning("获取 %s OI 数据失败: %s", symbol, e)
        return "N/A", "N/A"

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


#send 1m photo to a symbol
def send_symbol_1m_chart(symbol: str, market: str):
    """
    生成并发送某个 symbol 的 1 分钟 K 线截图
    """
    png_bytes = generate_1m_candlestick_png(symbol, market, limit=120)
    if png_bytes:
        send_telegram_photo(png_bytes)

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

        # 冷却时间，避免频繁提醒
        last_ts = last_alert_time[market].get(symbol, 0)
        if now_ts - last_ts < ALERT_COOLDOWN_SECONDS:
            continue

        last_alert_time[market][symbol] = now_ts

        # 24h 涨幅 & 成交额
        try:
            chg_24h = float(item.get("priceChangePercent", 0.0))
        except Exception:
            chg_24h = 0.0
        vol_quote = item.get("quoteVolume") or item.get("volume") or "0"

        # MC / FDV
        mc_str, fdv_str = get_mc_fdv_from_symbol(symbol)

        # OI & 15min OI 变化（只对合约市场有）
        if market in ("um", "cm"):
            oi_str, oi_15m_change = fetch_open_interest_and_change_15m(symbol, market)
        else:
            oi_str, oi_15m_change = "N/A", "N/A"

        # 方向
        direction = "📈 涨" if change_pct > 0 else "📉 跌"

        # 更好看的交易对展示
        pretty_symbol = symbol
        if symbol.endswith("USDT"):
            pretty_symbol = symbol.replace("USDT", "/USDT")

        tradingview_link = build_tradingview_1m_link(symbol, market)

        text_lines = [
            f"{direction} [{pretty_symbol}] {change_pct * 100:+.2f}% in {WINDOW_MINUTES} min",
            f"${base_price:.4f} → ${last_price:.4f}",
            f"24h: {chg_24h:+.2f}% | Vol: ${human_readable_number(vol_quote)}",
            f"MC: {mc_str} | FDV: {fdv_str} | OI: {oi_str}",
            f"{WINDOW_MINUTES} min 内 OI 变化: {oi_15m_change}",
            f"1m K线 (Binance): {tradingview_link}",
        ]
        msg = "\n".join(text_lines)
        logging.info("触发告警：%s", msg.replace("\n", " | "))
        send_telegram_message(msg)
        send_symbol_1m_chart(symbol,market)


def startup_message(spot_count: int, um_count: int, cm_count: int):
    """启动成功提示（推送到 TG）"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = (
        "✅ 运行成功！\n"
        f"当前时间: {now_str}\n"
        f"检测到 现货交易对: {spot_count} 个\n"
        f"U本位合约: {um_count} 个\n"
        f"币本位合约: {cm_count} 个\n"
        f"屏蔽币种(按 base asset): {', '.join(sorted(BLOCKED_BASES)) if BLOCKED_BASES else '无'}"
    )
    logging.info(text.replace("\n", " | "))
    send_telegram_message(text)


# ================== 主循环 ==================

def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("请先在 .env 中配置 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID ！")
        # 仍然允许运行，只是不能发消息

    load_coingecko_marketcaps()

    # 先拉一次数据，统计数量并发“运行成功”提示
    spot_tickers = fetch_spot_24h_tickers()
    um_tickers = fetch_futures_24h_tickers("um")
    cm_tickers = fetch_futures_24h_tickers("cm")

    startup_message(len(spot_tickers), len(um_tickers), len(cm_tickers))

    # 初始填充历史价格（让 15min 统计尽快生效）
    update_and_check_market("spot", spot_tickers)
    update_and_check_market("um", um_tickers)
    update_and_check_market("cm", cm_tickers)

    logging.info(
        "开始循环监控：窗口=%d 分钟，波动阈值=%.2f%%，循环间隔=%d 秒",
        WINDOW_MINUTES,
        PRICE_CHANGE_THRESHOLD * 100,
        CHECK_INTERVAL_SECONDS,
    )

    while True:
        loop_start = time.time()
        try:
            spot_tickers = fetch_spot_24h_tickers()
            update_and_check_market("spot", spot_tickers)
        except Exception as e:
            logging.warning("拉取现货数据失败: %s", e)

        try:
            um_tickers = fetch_futures_24h_tickers("um")
            update_and_check_market("um", um_tickers)
        except Exception as e:
            logging.warning("拉取 U 本位合约数据失败: %s", e)

        try:
            cm_tickers = fetch_futures_24h_tickers("cm")
            update_and_check_market("cm", cm_tickers)
        except Exception as e:
            logging.warning("拉取 币本位合约数据失败: %s", e)

        elapsed = time.time() - loop_start
        sleep_time = max(5, CHECK_INTERVAL_SECONDS - elapsed)
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
