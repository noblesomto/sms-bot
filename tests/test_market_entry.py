from scheduler import _check_rr
from alerts.formatter import format_signal_alert

def test_rr_from_market_price():
    # market 4036.70, SL 4021.57 → risk 15.13; TP1 4088.75 → reward 52.05 → RR 3.44
    assert _check_rr("LONG", 4036.70, 4088.75, 4021.57, min_rr=2.0)

def test_rr_rejects_when_market_price_degrades_rr():
    # zone-mid RR would pass, market-price RR must fail:
    # market 4035, SL 4021 → risk 14; TP1 4055 → reward 20 → RR 1.43 < 2
    assert not _check_rr("LONG", 4035.0, 4055.0, 4021.0, min_rr=2.0)

def test_rr_short():
    assert _check_rr("SHORT", 4036.0, 3993.25, 4043.7, min_rr=2.0)

def test_rr_invalid_geometry():
    assert not _check_rr("LONG", 4030.0, 4020.0, 4040.0)  # target below entry

def test_alert_shows_market_entry():
    msg = format_signal_alert(
        pair="XAU/USD", direction="LONG", timeframe="1h", session="LONDON_OPEN",
        confluence_score=7, factors=["x"], entry_low=4022.5, entry_high=4041.0,
        target1=4088.75, target2=4112.5, invalidation=4021.57,
        entry_price=4036.70,
    )
    assert "Entry (market): 4036.70" in msg
    assert "4022.50 – 4041.00" in msg  # zone still shown as context
