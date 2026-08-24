from datetime import datetime, timezone


def _fmt(price: float) -> str:
    """Format price with appropriate decimal places: 2 for gold/JPY, 5 for forex."""
    return f"{price:.2f}" if price >= 100 else f"{price:.5f}"


def format_signal_alert(
    pair: str,
    direction: str,
    timeframe: str,
    session: str,
    confluence_score: int,
    factors: list,
    entry_low: float,
    entry_high: float,
    target1: float,
    target2: float,
    invalidation: float,
    timestamp: datetime = None,
    max_score: int = 10,
    wyckoff_context: dict = None,
    entry_price: float = None,
) -> str:
    """
    Build a Telegram-ready alert message for an SMC/ICT + Wyckoff signal.

    LONG signals use the green circle emoji; SHORT signals use red.
    Wyckoff context is appended as a single descriptive line when present.
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    emoji = "🟢" if direction == "LONG" else "🔴"
    label = "LONG SIGNAL" if direction == "LONG" else "SHORT SIGNAL"
    session_display = session.replace("_", " ").title() if session else "N/A"
    ob_side = "low" if direction == "LONG" else "high"
    factors_text = "\n".join(f"   • {f}" for f in factors)

    # Wyckoff one-liner: only shown when Phase C event is confirmed
    wyckoff_line = ""
    if wyckoff_context and wyckoff_context.get("key_event"):
        wyckoff_line = f"📐 Wyckoff: {wyckoff_context['description']}\n\n"

    entry_line = (
        f"📍 Entry (market): {_fmt(entry_price)}\n"
        f"   OB zone: {_fmt(entry_low)} – {_fmt(entry_high)}\n\n"
        if entry_price is not None
        else f"📍 Entry Zone: {_fmt(entry_low)} – {_fmt(entry_high)}\n\n"
    )

    return (
        f"{emoji} {label} — {pair}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Timeframe: {timeframe.upper()} | Session: {session_display}\n\n"
        f"⚡ Confluence Score: {confluence_score}/{max_score}\n"
        f"✅ Factors:\n{factors_text}\n\n"
        f"{wyckoff_line}"
        f"{entry_line}"
        f"🎯 Target 1: {_fmt(target1)}\n\n"
        f"🎯 Target 2: {_fmt(target2)}\n\n"
        f"🛑 Stop Loss: {_fmt(invalidation)} (OB {ob_side})\n"
        f"⏰ {timestamp.strftime('%Y-%m-%d %H:%M')} UTC\n\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )


def format_tp_hit_alert(
    pair: str,
    direction: str,
    timeframe: str,
    tp_level: str,
    hit_price: float,
    entry: float,
    pnl_pips: float,
    target2: float = None,
) -> str:
    """Alert sent when TP1 or TP2 is reached."""
    emoji = "🎯" if tp_level == "TP1" else "💰"
    dir_emoji = "🟢" if direction == "LONG" else "🔴"
    pnl_sign = "+" if pnl_pips >= 0 else ""

    lines = [
        f"{emoji} {tp_level} HIT — {pair}",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"",
        f"{dir_emoji} Direction: {direction} | {timeframe.upper()}",
        f"📍 Entry: {_fmt(entry)}",
        f"✅ Hit Price: {_fmt(hit_price)}",
        f"📈 PnL: {pnl_sign}{pnl_pips} pips",
    ]
    if tp_level == "TP1" and target2:
        lines.append(f"")
        lines.append(f"👀 Watching for TP2: {_fmt(target2)}")
    lines += ["", "━━━━━━━━━━━━━━━━━━━━"]
    return "\n".join(lines)


def format_expiry_alert(pair, direction, timeframe, entry,
                        current_price, unrealized_pips, expiry_hours) -> str:
    """Sent when a signal expires with neither TP nor SL hit. The user enters
    at market on alert, so an expired signal is an open position they are
    still holding — tell them the bot has stopped tracking it."""
    dir_emoji = "🟢" if direction == "LONG" else "🔴"
    if current_price is not None:
        sign = "+" if unrealized_pips >= 0 else ""
        price_line = (f"📉 Current: {_fmt(current_price)} "
                      f"({sign}{unrealized_pips} pips unrealized)")
    else:
        price_line = "📉 Current: n/a"
    return "\n".join([
        f"⌛ SIGNAL EXPIRED — {pair}",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"{dir_emoji} Direction: {direction} | {timeframe.upper()}",
        f"📍 Entry: {_fmt(entry)}",
        price_line,
        "",
        f"No TP/SL hit within {expiry_hours}h — the bot is no longer tracking",
        "this signal. If you entered, close or manage the position manually.",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
    ])


def format_kill_switch_alert(n: int, mean_r: float, threshold: float,
                             lookback_days: int = 14) -> str:
    """Circuit-breaker alert (roadmap Phase 2½): the trailing resolved-signal
    expectancy has fallen to/below the kill threshold. Informational — it
    does not stop scanning, but demands a human decision."""
    return "\n".join([
        "🛑 KILL-SWITCH ALERT — SMC Bot",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"Trailing {n} resolved signals averaging {mean_r:+.2f}R "
        f"(threshold: {threshold}R)",
        "",
        "Expectancy has gone materially negative. Review recent signals in",
        "/dashboard, consider pausing scanning or tightening filters before",
        "the next trades are taken.",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
    ])


def test_formatter():
    """Standalone test — prints the formatted alert for a sample LONG signal."""
    msg = format_signal_alert(
        pair="XAUUSD",
        direction="LONG",
        timeframe="1h",
        session="LONDON_OPEN",
        confluence_score=7,
        factors=[
            "HTF Daily bias: BULLISH",
            "Price at Bullish OB (2318.40 – 2321.80)",
            "FVG present in zone",
            "Sell-side liquidity swept",
            "In kill zone",
            "Discount zone (below 50% EQ)",
            "CHoCH confirmed on LTF",
        ],
        entry_low=2318.40,
        entry_high=2321.80,
        target1=2338.00,
        target2=2355.00,
        invalidation=2314.00,
        timestamp=datetime(2024, 1, 15, 8, 32, tzinfo=timezone.utc),
    )
    print(msg)
    return msg


if __name__ == "__main__":
    test_formatter()
