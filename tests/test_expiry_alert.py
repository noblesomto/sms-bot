from alerts.formatter import format_expiry_alert

def test_expiry_alert_contents():
    msg = format_expiry_alert(pair="XAU/USD", direction="LONG", timeframe="1h",
                              entry=4036.70, current_price=4030.00,
                              unrealized_pips=-67.0, expiry_hours=48)
    assert "EXPIRED" in msg and "XAU/USD" in msg
    assert "4036.70" in msg and "4030.00" in msg
    assert "-67.0" in msg and "48h" in msg
    assert "no longer tracking" in msg.lower()

def test_expiry_alert_without_price():
    msg = format_expiry_alert(pair="XAU/USD", direction="LONG", timeframe="1h",
                              entry=4036.70, current_price=None,
                              unrealized_pips=None, expiry_hours=48)
    assert "n/a" in msg.lower()
