def price_precision(price: float) -> int:
    """Return appropriate decimal places based on price magnitude.

    2dp fits JPY-crosses, metals, and indices (their natural pip size is
    ~0.01-1). 5dp is required for 4-decimal forex pairs (EUR/USD, GBP/USD,
    AUD/USD, USD/CAD, GBP/CAD, EUR/CAD, ...) — rounding those to 2dp is
    ~100x coarser than a pip and collapses OB/FVG/liquidity zones (whose
    candle ranges are typically 5-30 pips) down to a single point.
    """
    if price >= 100:   # XAU/USD (~4000), USD/JPY (~160), NAS100/US30
        return 2
    return 5            # EUR/USD, GBP/USD, AUD/USD, USD/CAD, ... and sub-dollar pairs
