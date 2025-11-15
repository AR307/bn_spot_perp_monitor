import os
import time
import logging
from collections import defaultdict, deque
from datetime import datetime

import requests
from dotenv import load_dotenv

# ================== 基本配置 ==================

load_dotenv("profile.env")  # 默认读取当前目录的 .env 文件

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

PRICE_CHANGE_THRESHOLD = float(os.getenv("PRICE_CHANGE_THRESHOLD", "0.03"))  # 3%
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
WINDOW_MINUTES = int(os.getenv("WINDOW_MINUTES", "15"))
ALERT_COOLDOWN_SECONDS = WINDOW_MINUTES * 60  # 同一币种至少间隔一个窗口再提醒

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

def send_telegram_message(text: str):
    """发送 Telegram 消息"""
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
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if not resp.ok:
            logging.warning("发送 Telegram 失败: %s", resp.text)
    except Exception as e:
        logging.exception("发送 Telegram 异常: %s", e)


def human_readable_number(x):
    """数字缩写：2_800_000_000 -> 2.8B"""
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


def load_coingecko_marketcaps():
    """从 CoinGecko 拉一份 symbol -> (mc, fdv) 映射（粗略就够用）"""
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
    """
    U 本位和现货一般是 XXXUSDT，提取前面的 XXX 来匹配 CoinGecko symbol
    """
    base = binance_symbol
    for quote in ["USDT", "BUSD", "FDUSD", "USDC", "BTC"]:
        if base.endswith(quote):
            base = base[:-len(quote)]
            break
    base = base.upper()
    info = coingecko_cache.get(base)
    if not info:
        return "N/A", "N/A"
    return human_readable_number(info["mc"]), human_readable_number(info["fdv"])


# ================== Binance 数据拉取 ==================

def fetch_spot_24h_tickers():
    """现货 24h 行情"""
    url = f"{BINANCE_SPOT_BASE}/api/v3/ticker/24hr"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    # 只保留 USDT 交易对，并排除杠杆代币（UP/DOWN）
    result = []
    for item in data:
        symbol = item["symbol"]
        if not symbol.endswith("USDT"):
            continue
        if symbol.endswith("UPUSDT") or symbol.endswith("DOWNUSDT"):
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
    # 过滤掉交割合约以外的内容，可以按需要自己再过滤
    return data


def fetch_open_interest_and_change_15m(symbol: str, market: str):
    """
    获取当前 OI 和约 15 分钟内 OI 变化百分比
    只在触发提醒时调用，避免过多请求
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


# ================== 监控 & 告警逻辑 ==================

def update_and_check_market(market: str, tickers: list):
    """
    更新某个市场(spot/um/cm)的价格历史，并检查是否触发 15min 告警
    """
    now_ts = time.time()
    window_seconds = WINDOW_MINUTES * 60

    for item in tickers:
        symbol = item["symbol"]
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
            # 数据不足 15分钟，不检查
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

        text_lines = [
            f"{direction} [{pretty_symbol}] {change_pct * 100:+.2f}% in {WINDOW_MINUTES} min",
            f"${base_price:.4f} → ${last_price:.4f}",
            f"24h: {chg_24h:+.2f}% | Vol: ${human_readable_number(vol_quote)}",
            f"MC: {mc_str} | FDV: {fdv_str} | OI: {oi_str}",
            f"{WINDOW_MINUTES} min 内 OI 变化: {oi_15m_change}",
        ]
        msg = "\n".join(text_lines)
        logging.info("触发告警：%s", msg.replace("\n", " | "))
        send_telegram_message(msg)


def startup_message(spot_count: int, um_count: int, cm_count: int):
    """启动成功提示（推送到 TG）"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = (
        "✅ 运行成功！\n"
        f"当前时间: {now_str}\n"
        f"检测到 现货交易对: {spot_count} 个\n"
        f"U本位合约: {um_count} 个\n"
        f"币本位合约: {cm_count} 个"
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
