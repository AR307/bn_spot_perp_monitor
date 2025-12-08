"""
Binance Spot/Perpetual Monitor - Refactored for Performance
Monitors U-margined futures for price changes and sends Telegram alerts
"""
import asyncio
import logging
import time
from datetime import datetime
from typing import List, Dict, Any

from config import Config, get_oi_period_and_label
from cache_manager import CacheManager
from telegram_client import TelegramClient
from api_client import AsyncBinanceClient, AsyncCoinGeckoClient
from alert_manager import AlertManager
from models import AlertInfo
from utils import extract_base_asset, human_readable_number


# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


async def process_tickers(
    config: Config,
    cache: CacheManager,
    alert_manager: AlertManager,
    binance_client: AsyncBinanceClient,
    tickers: List[Dict[str, Any]],
    market: str,
    oi_period: str,
    threshold: float,
):
    """Process tickers and check for alerts"""
    now_ts = time.time()
    
    # Update price history for all tickers
    for item in tickers:
        symbol = item["symbol"]
        base_asset = extract_base_asset(symbol)
        
        # Double-check blacklist
        if base_asset in config.blocked_bases:
            continue
        
        try:
            last_price = float(item["lastPrice"])
        except (ValueError, KeyError):
            continue
        
        cache.update_price(market, symbol, now_ts, last_price)
    
    # Check for alerts
    alerts_to_send = []
    
    for item in tickers:
        symbol = item["symbol"]
        base_asset = extract_base_asset(symbol)
        
        if base_asset in config.blocked_bases:
            continue
        
        # Check price change
        result = cache.get_price_change(market, symbol)
        if result is None:
            continue
        
        change_pct, base_price, current_price = result
        
        if abs(change_pct) < threshold:
            continue
        
        # Check alert interval
        direction = "UP" if change_pct > 0 else "DOWN"
        if not cache.should_alert(base_asset, direction, now_ts, config.alert_min_interval_seconds):
            continue
        
        # Update streak
        alert_count, minutes_since_prev = cache.update_alert_streak(base_asset, direction, now_ts)
        
        # Gather data for alert
        try:
            chg_24h = float(item.get("priceChangePercent", 0.0))
        except (ValueError, TypeError):
            chg_24h = 0.0
        
        vol_quote = item.get("quoteVolume") or item.get("volume") or "0"
        vol_quote = human_readable_number(vol_quote)
        
        # MC / FDV
        mc_str, fdv_str, mc_raw, fdv_raw = cache.get_mc_fdv(symbol)
        
        # Create alert info (we'll fetch OI in parallel later)
        alerts_to_send.append({
            "symbol": symbol,
            "base_asset": base_asset,
            "change_pct": change_pct,
            "base_price": base_price,
            "current_price": current_price,
            "direction": direction,
            "alert_count": alert_count,
            "minutes_since_prev": minutes_since_prev,
            "chg_24h": chg_24h,
            "vol_quote": vol_quote,
            "mc_str": mc_str,
            "fdv_str": fdv_str,
            "mc_raw": mc_raw,
            "fdv_raw": fdv_raw,
        })
    
    # Fetch OI data in parallel for all alerts
    if alerts_to_send:
        oi_tasks = [
            binance_client.fetch_open_interest_stats(alert["symbol"], market, oi_period)
            for alert in alerts_to_send
        ]
        oi_results = await asyncio.gather(*oi_tasks)
        
        # Send alerts
        for alert_data, (oi_str, oi_change_str, oi_value_usd) in zip(alerts_to_send, oi_results):
            alert = AlertInfo(
                symbol=alert_data["symbol"],
                base_asset=alert_data["base_asset"],
                change_pct=alert_data["change_pct"],
                base_price=alert_data["base_price"],
                current_price=alert_data["current_price"],
                direction=alert_data["direction"],
                alert_count=alert_data["alert_count"],
                minutes_since_prev=alert_data["minutes_since_prev"],
                chg_24h=alert_data["chg_24h"],
                vol_quote=alert_data["vol_quote"],
                mc_str=alert_data["mc_str"],
                fdv_str=alert_data["fdv_str"],
                mc_raw=alert_data["mc_raw"],
                fdv_raw=alert_data["fdv_raw"],
                oi_str=oi_str,
                oi_change_str=oi_change_str,
                oi_value_usd=oi_value_usd,
            )
            
            await alert_manager.send_alert(binance_client, alert, market)


async def main_loop():
    """Main monitoring loop"""
    # Load configuration
    config = Config.from_env()
    
    if not config.telegram_bot_token or not config.telegram_chat_id:
        logging.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in profile.env")
        return
    
    # Get OI period configuration
    oi_period, oi_window_label, oi_window_minutes = get_oi_period_and_label(config.oi_window_minutes)
    
    # Initialize components
    cache = CacheManager(config.window_minutes, config.alert_reset_seconds)
    telegram = TelegramClient(config.telegram_bot_token, config.telegram_chat_id)
    alert_manager = AlertManager(cache, telegram, config.window_minutes, oi_period, oi_window_label)
    
    # Load CoinGecko data
    async with AsyncCoinGeckoClient() as coingecko_client:
        cg_cache = await coingecko_client.load_marketcaps()
        cache.update_coingecko_cache(cg_cache)
    
    # Initial ticker fetch
    async with AsyncBinanceClient(config.blocked_bases) as binance_client:
        um_tickers = await binance_client.fetch_futures_24h_tickers("um")
        
        # Send startup message
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        startup_text = (
            "✅ 监控系统运行成功！\n"
            f"当前时间: {now_str}\n"
            f"监控模式: 仅 U 本位合约\n"
            f"检测到 U 本位合约: {len(um_tickers)} 个\n"
            f"屏蔽币种(按 base asset): {', '.join(sorted(config.blocked_bases)) if config.blocked_bases else '无'}\n"
            f"CoinGecko 缓存: {len(cache.coingecko_cache)} 个 symbol"
        )
        logging.info(startup_text.replace("\n", " | "))
        await telegram.send_message(startup_text)
        
        # Initial price history population
        now_ts = time.time()
        for item in um_tickers:
            symbol = item["symbol"]
            try:
                last_price = float(item["lastPrice"])
                cache.update_price("um", symbol, now_ts, last_price)
            except (ValueError, KeyError):
                continue
        
        logging.info(
            "开始循环监控：仅 U 本位合约，窗口=%d 分钟，波动阈值=%.2f%%，循环间隔=%d 秒",
            config.window_minutes,
            config.price_change_threshold * 100,
            config.check_interval_seconds,
        )
    
    # Main monitoring loop
    while True:
        loop_start = time.time()
        
        try:
            # Refresh CoinGecko cache if needed
            if cache.should_refresh_coingecko(config.coingecko_refresh_interval):
                logging.info(
                    "CoinGecko cache expired (%d seconds), refreshing...",
                    config.coingecko_refresh_interval
                )
                async with AsyncCoinGeckoClient() as coingecko_client:
                    cg_cache = await coingecko_client.load_marketcaps()
                    cache.update_coingecko_cache(cg_cache)
            
            # Fetch tickers and process
            async with AsyncBinanceClient(config.blocked_bases) as binance_client:
                um_tickers = await binance_client.fetch_futures_24h_tickers("um")
                
                await process_tickers(
                    config,
                    cache,
                    alert_manager,
                    binance_client,
                    um_tickers,
                    "um",
                    oi_period,
                    config.price_change_threshold,
                )
        
        except Exception as e:
            logging.exception("Error in main loop: %s", e)
        
        # Sleep until next check
        elapsed = time.time() - loop_start
        sleep_time = max(5, config.check_interval_seconds - elapsed)
        await asyncio.sleep(sleep_time)


def main():
    """Entry point"""
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logging.info("Shutting down gracefully...")


if __name__ == "__main__":
    main()
