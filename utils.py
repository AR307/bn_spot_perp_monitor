"""
Utility functions for the Binance monitor
"""


def extract_base_asset(binance_symbol: str) -> str:
    """
    Extract base asset from Binance symbol
    
    Examples:
    - BTCUSDT      -> BTC
    - ETHFDUSD     -> ETH
    - BTCUSD_PERP  -> BTC
    """
    base = binance_symbol
    
    # Remove _PERP suffix (coin-margined perpetual)
    if base.endswith("_PERP"):
        base = base[:-5]
    
    # Remove common quote currency suffixes
    for quote in ["USDT", "BUSD", "FDUSD", "USDC", "BTC", "USD"]:
        if base.endswith(quote):
            base = base[: -len(quote)]
            break
    
    return base.upper()


def human_readable_number(x) -> str:
    """
    Format number with abbreviations: 2800000000 -> 2.8B
    """
    try:
        x = float(x)
    except (ValueError, TypeError):
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


def build_tradingview_link(binance_symbol: str) -> str:
    """
    Build TradingView 1-minute chart link
    https://www.tradingview.com/chart/?symbol=BINANCE:BTCUSDT.P&interval=1
    """
    return f"https://www.tradingview.com/chart/?symbol=BINANCE:{binance_symbol}.P&interval=1"
