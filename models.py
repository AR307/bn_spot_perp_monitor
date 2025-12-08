"""
Data models for the Binance monitor
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MarketCapInfo:
    """Market cap and FDV information from CoinGecko"""
    mc: Optional[float] = None
    fdv: Optional[float] = None


@dataclass
class OpenInterestData:
    """Open Interest statistics"""
    current_oi_usd: float
    change_pct: Optional[float] = None
    
    def __post_init__(self):
        """Validate data"""
        if self.current_oi_usd < 0:
            raise ValueError("OI cannot be negative")


@dataclass
class TickerData:
    """Binance ticker information"""
    symbol: str
    last_price: float
    price_change_percent_24h: float
    quote_volume: str
    
    @property
    def base_asset(self) -> str:
        """Extract base asset from symbol"""
        from utils import extract_base_asset
        return extract_base_asset(self.symbol)


@dataclass
class AlertStreak:
    """Track alert streak for a base asset"""
    last_dir: Optional[str] = None  # "UP" or "DOWN"
    up_count: int = 0
    down_count: int = 0
    last_up_ts: float = 0.0
    last_down_ts: float = 0.0


@dataclass
class AlertInfo:
    """Information for generating an alert"""
    symbol: str
    base_asset: str
    change_pct: float
    base_price: float
    current_price: float
    direction: str  # "UP" or "DOWN"
    alert_count: int
    minutes_since_prev: Optional[float]
    
    # 24h stats
    chg_24h: float
    vol_quote: str
    
    # Market data
    mc_str: str
    fdv_str: str
    mc_raw: Optional[float]
    fdv_raw: Optional[float]
    
    # OI data
    oi_str: str
    oi_change_str: str
    oi_value_usd: Optional[float]
    
    @property
    def oi_mc_ratio_str(self) -> str:
        """Calculate OI/MC ratio"""
        if (self.oi_value_usd is not None and self.oi_value_usd > 0 
            and self.mc_raw is not None and self.mc_raw > 0):
            try:
                ratio = self.oi_value_usd / self.mc_raw
                return f"{ratio * 100:.2f}%"
            except Exception:
                pass
        return "N/A"
    
    @property
    def direction_emoji(self) -> str:
        """Get direction emoji"""
        return "📈 涨" if self.direction == "UP" else "📉 跌"
    
    @property
    def direction_cn(self) -> str:
        """Get Chinese direction text"""
        return "上涨" if self.direction == "UP" else "下跌"
