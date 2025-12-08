"""
Configuration management for Binance spot/perp monitor
Loads and validates all environment variables
"""
import os
from dataclasses import dataclass
from typing import Set
from dotenv import load_dotenv


@dataclass
class Config:
    """Application configuration"""
    
    # Telegram settings
    telegram_bot_token: str
    telegram_chat_id: str
    
    # Monitoring parameters
    price_change_threshold: float  # e.g., 0.03 for 3%
    check_interval_seconds: int
    window_minutes: int
    
    # Alert settings
    alert_min_interval_seconds: int
    alert_reset_seconds: int
    
    # OI (Open Interest) settings
    oi_window_minutes: int
    
    # CoinGecko settings
    coingecko_refresh_interval: int  # seconds
    
    # Blacklist
    blocked_bases: Set[str]
    
    @classmethod
    def from_env(cls, env_file: str = "profile.env") -> "Config":
        """Load configuration from environment file"""
        load_dotenv(env_file)
        
        # Parse blacklist
        blacklist_bases = os.getenv("BLACKLIST_BASES", "BTTC")
        blocked_bases = {b.strip().upper() for b in blacklist_bases.split(",") if b.strip()}
        
        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            price_change_threshold=float(os.getenv("PRICE_CHANGE_THRESHOLD", "0.03")),
            check_interval_seconds=int(os.getenv("CHECK_INTERVAL_SECONDS", "60")),
            window_minutes=int(os.getenv("WINDOW_MINUTES", "15")),
            alert_min_interval_seconds=int(os.getenv("ALERT_MIN_INTERVAL_SECONDS", "60")),
            alert_reset_seconds=int(os.getenv("ALERT_RESET_SECONDS", "1800")),
            oi_window_minutes=int(os.getenv("OI_WINDOW_MINUTES", "15")),
            coingecko_refresh_interval=int(os.getenv("COINGECKO_REFRESH_INTERVAL", "21600")),
            blocked_bases=blocked_bases,
        )


# API endpoints
BINANCE_SPOT_BASE = "https://api.binance.com"
BINANCE_FAPI_BASE = "https://fapi.binance.com"  # U-margined
BINANCE_DAPI_BASE = "https://dapi.binance.com"  # Coin-margined

# OI period mapping (minutes -> Binance API period)
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


def get_oi_period_and_label(window_minutes: int) -> tuple[str, str, int]:
    """
    Map minutes to Binance period and label
    Returns: (period, label, actual_minutes)
    """
    if window_minutes in OI_PERIOD_MAP:
        actual_minutes = window_minutes
        period = OI_PERIOD_MAP[window_minutes]
    else:
        # Find closest match
        closest = min(OI_PERIOD_MAP.keys(), key=lambda k: abs(k - window_minutes))
        actual_minutes = closest
        period = OI_PERIOD_MAP[closest]
    
    # Format label
    if actual_minutes < 60:
        label = f"{actual_minutes} min"
    elif actual_minutes == 1440:
        label = "1 d"
    elif actual_minutes % 60 == 0:
        label = f"{actual_minutes // 60} h"
    else:
        label = f"{actual_minutes} min"
    
    return period, label, actual_minutes
