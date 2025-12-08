"""
Async API clients for Binance and CoinGecko
"""
import logging
from typing import List, Dict, Any, Optional
import aiohttp
import asyncio

from config import BINANCE_SPOT_BASE, BINANCE_FAPI_BASE, BINANCE_DAPI_BASE
from utils import extract_base_asset


class AsyncBinanceClient:
    """Async Binance API client with connection pooling"""
    
    def __init__(self, blocked_bases: set, max_connections: int = 100):
        self.blocked_bases = blocked_bases
        self.connector = aiohttp.TCPConnector(limit=max_connections)
        
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(connector=self.connector)
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.session.close()
        await self.connector.close()
    
    async def fetch_futures_24h_tickers(self, market: str) -> List[Dict[str, Any]]:
        """
        Fetch 24h futures tickers
        market: 'um' (U-margined) or 'cm' (coin-margined)
        """
        if market == "um":
            base = BINANCE_FAPI_BASE
            url = f"{base}/fapi/v1/ticker/24hr"
        else:  # cm
            base = BINANCE_DAPI_BASE
            url = f"{base}/dapi/v1/ticker/24hr"
        
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resp.raise_for_status()
                data = await resp.json()
                
                # Filter blacklisted symbols
                filtered = []
                for item in data:
                    symbol = item["symbol"]
                    base_asset = extract_base_asset(symbol)
                    if base_asset not in self.blocked_bases:
                        filtered.append(item)
                
                return filtered
        except Exception as e:
            logging.warning("Failed to fetch %s futures tickers: %s", market, e)
            return []
    
    async def fetch_open_interest_stats(
        self, 
        symbol: str, 
        market: str, 
        period: str,
        retry: bool = True
    ) -> tuple[str, str, Optional[float]]:
        """
        Fetch OI stats for a symbol
        Returns: (oi_str, oi_change_str, oi_value_usd)
        """
        try:
            if market == "um":
                base = BINANCE_FAPI_BASE
            else:  # cm
                base = BINANCE_DAPI_BASE
            
            url = f"{base}/futures/data/openInterestHist"
            params = {"symbol": symbol, "period": period, "limit": 2}
            
            async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
                hist = await resp.json()
                
                if not hist:
                    return "N/A", "N/A", None
                
                latest = hist[-1]
                current_oi_value = float(latest.get("sumOpenInterestValue", 0.0) or 0.0)
                
                # Calculate change
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
                
                from utils import human_readable_number
                oi_display_str = "$" + human_readable_number(current_oi_value)
                return oi_display_str, change_str, current_oi_value
                
        except Exception as e:
            logging.warning("Failed to fetch OI for %s: %s", symbol, e)
            if retry:
                await asyncio.sleep(0.5)
                return await self.fetch_open_interest_stats(symbol, market, period, retry=False)
            return "N/A", "N/A", None
    
    async def fetch_1m_klines(self, symbol: str, market: str, limit: int = 240) -> List[List]:
        """
        Fetch 1-minute klines
        Returns list of klines or empty list on error
        """
        if market == "spot":
            base = BINANCE_SPOT_BASE
            path = "/api/v3/klines"
        elif market == "um":
            base = BINANCE_FAPI_BASE
            path = "/fapi/v1/klines"
        else:  # cm
            base = BINANCE_DAPI_BASE
            path = "/dapi/v1/klines"
        
        url = base + path
        params = {"symbol": symbol, "interval": "1m", "limit": limit}
        
        try:
            async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resp.raise_for_status()
                return await resp.json()
        except Exception as e:
            logging.warning("Failed to fetch klines for %s: %s", symbol, e)
            return []


class AsyncCoinGeckoClient:
    """Async CoinGecko API client"""
    
    def __init__(self):
        self.connector = aiohttp.TCPConnector(limit=10)
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(connector=self.connector)
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.session.close()
        await self.connector.close()
    
    async def load_marketcaps(self, max_pages: int = 10) -> Dict[str, Dict[str, Optional[float]]]:
        """
        Load market caps from CoinGecko
        Returns: {symbol: {"mc": float, "fdv": float}}
        """
        logging.info("Fetching market data from CoinGecko...")
        cache = {}
        
        url = "https://api.coingecko.com/api/v3/coins/markets"
        
        for page in range(1, max_pages + 1):
            params = {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 250,
                "page": page,
                "sparkline": "false",
            }
            
            try:
                async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    
                    if not data:
                        break
                    
                    for coin in data:
                        symbol = str(coin.get("symbol", "")).upper()
                        mc = coin.get("market_cap")
                        fdv = coin.get("fully_diluted_valuation")
                        
                        # Keep highest market cap for duplicate symbols
                        if symbol not in cache or (mc or 0) > (cache[symbol]["mc"] or 0):
                            cache[symbol] = {"mc": mc, "fdv": fdv}
            
            except Exception as e:
                logging.warning("Failed to fetch CoinGecko page %d: %s", page, e)
                break
        
        logging.info("CoinGecko cache loaded: %d symbols", len(cache))
        return cache
