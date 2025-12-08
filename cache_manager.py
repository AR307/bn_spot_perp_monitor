"""
Cache manager for price history, market caps, and alert state
"""
import time
import logging
from collections import defaultdict, deque
from typing import Dict, Optional, Tuple
from models import AlertStreak, MarketCapInfo
from utils import extract_base_asset, human_readable_number


class CacheManager:
    """Centralized cache management"""
    
    def __init__(self, window_minutes: int, alert_reset_seconds: int):
        self.window_minutes = window_minutes
        self.window_seconds = window_minutes * 60
        self.alert_reset_seconds = alert_reset_seconds
        
        # Price history: market -> symbol -> deque of (timestamp, price)
        self.price_history: Dict[str, Dict[str, deque]] = {
            "um": defaultdict(lambda: deque(maxlen=100)),  # Size-limited
        }
        
        # Alert streak state: base_asset -> AlertStreak
        self.alert_streak_state: Dict[str, AlertStreak] = {}
        
        # Last alert time for (base + direction): "BTC:UP" -> timestamp
        self.last_alert_key_time: Dict[str, float] = {}
        
        # Last message ID for replies: "BTC:UP" -> message_id
        self.alert_last_message_id: Dict[str, int] = {}
        
        # CoinGecko cache: symbol -> MarketCapInfo
        self.coingecko_cache: Dict[str, MarketCapInfo] = {}
        self.last_coingecko_update: float = 0
    
    def update_price(self, market: str, symbol: str, timestamp: float, price: float):
        """Add price point and clean old data"""
        history = self.price_history[market][symbol]
        history.append((timestamp, price))
        
        # Remove data outside window (deque maxlen handles size, but we also filter by time)
        while history and (timestamp - history[0][0] > self.window_seconds):
            history.popleft()
    
    def get_price_change(self, market: str, symbol: str) -> Optional[Tuple[float, float, float]]:
        """
        Get price change for symbol
        Returns: (change_pct, base_price, current_price) or None
        """
        history = self.price_history[market].get(symbol)
        if not history or len(history) < 2:
            return None
        
        base_ts, base_price = history[0]
        current_ts, current_price = history[-1]
        
        if base_price <= 0:
            return None
        
        change_pct = (current_price - base_price) / base_price
        return change_pct, base_price, current_price
    
    def should_alert(
        self, 
        base_asset: str, 
        direction: str, 
        now_ts: float, 
        min_interval: int
    ) -> bool:
        """
        Check if we should alert for this base+direction
        Returns True if enough time has passed
        """
        alert_key = f"{base_asset}:{direction}"
        last_ts = self.last_alert_key_time.get(alert_key, 0)
        
        if now_ts - last_ts < min_interval:
            return False
        
        self.last_alert_key_time[alert_key] = now_ts
        return True
    
    def update_alert_streak(
        self, 
        base_asset: str, 
        direction: str, 
        now_ts: float
    ) -> Tuple[int, Optional[float]]:
        """
        Update alert streak and return (count, minutes_since_prev)
        """
        state = self.alert_streak_state.get(base_asset)
        if state is None:
            state = AlertStreak()
            self.alert_streak_state[base_asset] = state
        
        if direction == "UP":
            prev_ts = state.last_up_ts or 0.0
            minutes_since_prev = None if prev_ts == 0 else (now_ts - prev_ts) / 60.0
            
            # Reset if direction changed or timeout
            reset_needed = (
                state.last_dir != "UP" or 
                prev_ts == 0.0 or 
                now_ts - prev_ts > self.alert_reset_seconds
            )
            
            if reset_needed:
                state.up_count = 1
            else:
                state.up_count += 1
            
            state.last_up_ts = now_ts
            state.last_dir = "UP"
            count = state.up_count
        else:  # DOWN
            prev_ts = state.last_down_ts or 0.0
            minutes_since_prev = None if prev_ts == 0 else (now_ts - prev_ts) / 60.0
            
            reset_needed = (
                state.last_dir != "DOWN" or 
                prev_ts == 0.0 or 
                now_ts - prev_ts > self.alert_reset_seconds
            )
            
            if reset_needed:
                state.down_count = 1
            else:
                state.down_count += 1
            
            state.last_down_ts = now_ts
            state.last_dir = "DOWN"
            count = state.down_count
        
        return count, minutes_since_prev
    
    def get_last_message_id(self, base_asset: str, direction: str) -> Optional[int]:
        """Get last message ID for reply threading"""
        alert_key = f"{base_asset}:{direction}"
        return self.alert_last_message_id.get(alert_key)
    
    def set_last_message_id(self, base_asset: str, direction: str, message_id: int):
        """Store message ID for reply threading"""
        alert_key = f"{base_asset}:{direction}"
        self.alert_last_message_id[alert_key] = message_id
    
    def update_coingecko_cache(self, cache: Dict[str, Dict[str, Optional[float]]]):
        """Update CoinGecko cache"""
        self.coingecko_cache = {
            symbol: MarketCapInfo(mc=data["mc"], fdv=data["fdv"])
            for symbol, data in cache.items()
        }
        self.last_coingecko_update = time.time()
        logging.info("CoinGecko cache updated: %d symbols", len(self.coingecko_cache))
    
    def should_refresh_coingecko(self, refresh_interval: int) -> bool:
        """Check if CoinGecko cache should be refreshed"""
        return time.time() - self.last_coingecko_update > refresh_interval
    
    def get_mc_fdv(self, binance_symbol: str) -> Tuple[str, str, Optional[float], Optional[float]]:
        """
        Get MC and FDV for a symbol
        Returns: (mc_str, fdv_str, mc_val, fdv_val)
        """
        base = extract_base_asset(binance_symbol)
        info = self.coingecko_cache.get(base)
        
        if not info:
            return "N/A", "N/A", None, None
        
        mc_val = info.mc
        fdv_val = info.fdv
        
        return human_readable_number(mc_val), human_readable_number(fdv_val), mc_val, fdv_val
