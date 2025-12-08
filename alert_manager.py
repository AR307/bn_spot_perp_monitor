"""
Alert manager for detecting price changes and generating notifications
"""
import asyncio
import logging
from typing import Optional, List
from io import BytesIO
from datetime import datetime

import matplotlib
matplotlib.use('Agg')  # Non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from models import AlertInfo
from cache_manager import CacheManager
from telegram_client import TelegramClient
from api_client import AsyncBinanceClient
from utils import build_tradingview_link, extract_base_asset


class AlertManager:
    """Manages alert detection and notification"""
    
    def __init__(
        self,
        cache: CacheManager,
        telegram: TelegramClient,
        window_minutes: int,
        oi_period: str,
        oi_window_label: str,
    ):
        self.cache = cache
        self.telegram = telegram
        self.window_minutes = window_minutes
        self.oi_period = oi_period
        self.oi_window_label = oi_window_label
    
    async def generate_1m_candlestick_png(
        self,
        binance_client: AsyncBinanceClient,
        symbol: str,
        market: str,
        limit: int = 120
    ) -> Optional[bytes]:
        """
        Generate 1-minute candlestick chart PNG
        Runs in thread pool to avoid blocking
        """
        try:
            # Fetch klines
            klines = await binance_client.fetch_1m_klines(symbol, market, limit)
            if not klines:
                return None
            
            # Generate chart in thread pool (matplotlib is CPU-bound)
            return await asyncio.get_event_loop().run_in_executor(
                None, self._generate_chart_sync, klines, symbol
            )
        except Exception as e:
            logging.warning("Failed to generate chart for %s: %s", symbol, e)
            return None
    
    def _generate_chart_sync(self, klines: List[List], symbol: str) -> bytes:
        """Synchronous chart generation (runs in thread pool)"""
        times = []
        opens = []
        highs = []
        lows = []
        closes = []
        
        for k in klines:
            ts = datetime.fromtimestamp(k[0] / 1000.0)
            t = mdates.date2num(ts)
            times.append(t)
            opens.append(float(k[1]))
            highs.append(float(k[2]))
            lows.append(float(k[3]))
            closes.append(float(k[4]))
        
        fig, ax = plt.subplots(figsize=(10, 4))
        
        # Colors: green for up, red for down
        up_color = "#26a69a"
        down_color = "#ef5350"
        
        for t, o, h, l, c in zip(times, opens, highs, lows, closes):
            color = up_color if c >= o else down_color
            # High-low line
            ax.vlines(t, l, h, linewidth=1, color=color)
            # Body
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
    
    def format_alert_message(self, alert: AlertInfo) -> str:
        """Format alert message text"""
        # Pretty symbol formatting
        pretty_symbol = alert.symbol
        if alert.symbol.endswith("USDT"):
            pretty_symbol = alert.symbol.replace("USDT", "/USDT")
        
        # Last alert time text
        if alert.minutes_since_prev is None:
            last_alert_text = "上一次同方向告警: 首次告警"
        else:
            last_alert_text = f"上一次同方向告警: {alert.minutes_since_prev:.1f} 分钟前"
        
        tradingview_link = build_tradingview_link(alert.symbol)
        
        text_lines = [
            f"{alert.direction_emoji} [{pretty_symbol}] {alert.change_pct * 100:+.2f}% in {self.window_minutes} min | {alert.direction_cn}第 {alert.alert_count} 次告警",
            f"${alert.base_price:.4f} → ${alert.current_price:.4f}",
            f"24h: {alert.chg_24h:+.2f}% | Vol: ${alert.vol_quote}",
            f"MC: {alert.mc_str} | FDV: {alert.fdv_str} | OI: {alert.oi_str} | OI/MC: {alert.oi_mc_ratio_str}",
            f"{self.oi_window_label} 内 OI 变化: {alert.oi_change_str}",
            last_alert_text,
            f"1m K线 (TradingView): {tradingview_link}",
        ]
        
        return "\n".join(text_lines)
    
    async def send_alert(
        self,
        binance_client: AsyncBinanceClient,
        alert: AlertInfo,
        market: str
    ):
        """Send alert notification with chart"""
        msg = self.format_alert_message(alert)
        logging.info("Triggering alert: %s", msg.replace("\n", " | "))
        
        # Get reply-to message ID if this is a continuation
        prev_msg_id = None
        if alert.alert_count > 1:
            prev_msg_id = self.cache.get_last_message_id(alert.base_asset, alert.direction)
        
        # Generate chart (async, non-blocking)
        chart_bytes = await self.generate_1m_candlestick_png(
            binance_client, alert.symbol, market, limit=240
        )
        
        # Send notification
        if chart_bytes:
            message_id = await self.telegram.send_photo(
                chart_bytes, caption=msg, reply_to_message_id=prev_msg_id
            )
        else:
            message_id = await self.telegram.send_message(
                msg, reply_to_message_id=prev_msg_id
            )
        
        # Store message ID for threading
        if message_id is not None:
            self.cache.set_last_message_id(alert.base_asset, alert.direction, message_id)
