import glob
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import plotly.graph_objects as go

logger = logging.getLogger(__name__)

CHART_DIR = "/tmp"
CHART_MAX_AGE_HOURS = 1


def generate_chart(
    df: pd.DataFrame,
    pair: str,
    obs: list,
    fvgs: list,
    liquidity_levels: dict,
    bos_events: list,
    choch_events: list,
    signal: dict,
    wyckoff_context: Optional[dict] = None,
    data_note: Optional[str] = None,
) -> Optional[str]:
    """
    Render an annotated candlestick chart using Plotly and save it as a PNG.

    Overlaid layers (bottom to top):
      • OB zones: green (bullish) or red (bearish) shaded rectangles
      • FVG zones: blue shaded rectangles
      • EQH/EQL: purple/orange dashed horizontal lines
      • PDH/PDL: orange solid horizontal lines
      • BOS labels: white annotations with arrows
      • CHoCH labels: gold annotations with arrows
      • Signal entry box: highlighted border with ENTRY label

    Saves to /tmp/chart_{pair}_{timestamp}.png (kaleido required).
    Returns the file path, or None on failure.
    """
    df = df.tail(100).copy()

    fig = go.Figure()

    fig.add_trace(go.Candlestick(
        x=df["datetime"],
        open=df["open"],
        high=df["high"],
        low=df["low"],
        close=df["close"],
        name=pair,
        increasing_line_color="#26a69a",
        decreasing_line_color="#ef5350",
    ))

    for ob in obs:
        if ob.get("mitigated"):
            continue
        fill = "rgba(38,166,154,0.18)" if ob["direction"] == "BULLISH" else "rgba(239,83,80,0.18)"
        line = "#26a69a" if ob["direction"] == "BULLISH" else "#ef5350"
        label = "Bull OB" if ob["direction"] == "BULLISH" else "Bear OB"
        fig.add_hrect(
            y0=ob["ob_low"], y1=ob["ob_high"],
            fillcolor=fill, line_color=line, line_width=1,
            annotation_text=label, annotation_position="right",
            annotation_font_color=line,
        )

    for fvg in fvgs:
        if fvg.get("status") == "FILLED":
            continue
        fig.add_hrect(
            y0=fvg["fvg_low"], y1=fvg["fvg_high"],
            fillcolor="rgba(41,182,246,0.12)",
            line_color="#29b6f6", line_width=1,
            annotation_text="FVG", annotation_position="right",
            annotation_font_color="#29b6f6",
        )

    for level in liquidity_levels.get("eqh_levels", []):
        fig.add_hline(y=level, line_dash="dash", line_color="#9c27b0", line_width=1,
                      annotation_text="EQH", annotation_position="left",
                      annotation_font_color="#9c27b0")

    for level in liquidity_levels.get("eql_levels", []):
        fig.add_hline(y=level, line_dash="dash", line_color="#ff9800", line_width=1,
                      annotation_text="EQL", annotation_position="left",
                      annotation_font_color="#ff9800")

    if liquidity_levels.get("pdh"):
        fig.add_hline(y=liquidity_levels["pdh"], line_color="#ff9800", line_width=1.5,
                      annotation_text="PDH", annotation_position="right",
                      annotation_font_color="#ff9800")

    if liquidity_levels.get("pdl"):
        fig.add_hline(y=liquidity_levels["pdl"], line_color="#ff9800", line_width=1.5,
                      annotation_text="PDL", annotation_position="right",
                      annotation_font_color="#ff9800")

    for event in bos_events[-5:]:
        dt = event.get("datetime")
        if dt is not None:
            fig.add_annotation(
                x=dt, y=event["price"], text="BOS",
                showarrow=True, arrowhead=2,
                arrowcolor="#ffffff",
                font=dict(color="#ffffff", size=10),
                bgcolor="#444444",
            )

    for event in choch_events[-3:]:
        dt = event.get("datetime")
        if dt is not None:
            fig.add_annotation(
                x=dt, y=event["price"], text="CHoCH",
                showarrow=True, arrowhead=2,
                arrowcolor="#ffd700",
                font=dict(color="#ffd700", size=10),
                bgcolor="#333333",
            )

    if signal and signal.get("entry_low") and signal.get("entry_high"):
        fill = (
            "rgba(38,166,154,0.30)"
            if signal.get("direction") == "LONG"
            else "rgba(239,83,80,0.30)"
        )
        fig.add_hrect(
            y0=signal["entry_low"], y1=signal["entry_high"],
            fillcolor=fill, line_color="#ffd700", line_width=2,
            annotation_text="ENTRY", annotation_position="right",
            annotation_font_color="#ffd700",
        )

    if signal and signal.get("invalidation"):
        fig.add_hline(
            y=signal["invalidation"], line_dash="dash", line_color="#ef5350", line_width=1.5,
            annotation_text=f"SL {signal['invalidation']}", annotation_position="right",
            annotation_font_color="#ef5350",
        )

    # ── Wyckoff overlays ─────────────────────────────────────────────────
    if wyckoff_context:
        tr = wyckoff_context.get("trading_range")
        spring = wyckoff_context.get("spring")
        utad = wyckoff_context.get("utad")

        # Shade the trading range with a subtle grey background
        if tr:
            fig.add_hrect(
                y0=tr["range_low"], y1=tr["range_high"],
                fillcolor="rgba(150,150,150,0.08)",
                line_color="rgba(150,150,150,0.3)", line_width=1,
                annotation_text="Wyckoff Range",
                annotation_position="left",
                annotation_font_color="#8b949e",
                annotation_font_size=9,
            )

        # Mark Spring — triangle pointing up at the spring low
        if spring and spring.get("detected") and not df.empty:
            spring_dt = df["datetime"].iloc[-3]  # approximate timing
            fig.add_annotation(
                x=spring_dt, y=spring["spring_low"],
                text="▲ SPRING",
                showarrow=False,
                font=dict(color="#26a69a", size=11, family="monospace"),
                bgcolor="rgba(38,166,154,0.15)",
                bordercolor="#26a69a",
                borderwidth=1,
                yshift=-18,
            )

        # Mark UTAD — triangle pointing down at the utad high
        if utad and utad.get("detected") and not df.empty:
            utad_dt = df["datetime"].iloc[-3]
            fig.add_annotation(
                x=utad_dt, y=utad["utad_high"],
                text="▼ UTAD",
                showarrow=False,
                font=dict(color="#ef5350", size=11, family="monospace"),
                bgcolor="rgba(239,83,80,0.15)",
                bordercolor="#ef5350",
                borderwidth=1,
                yshift=18,
            )

    # Pin y-axis to candle data range so annotation lines don't force zoom-out
    y_min = float(df["low"].min())
    y_max = float(df["high"].max())
    pad = (y_max - y_min) * 0.05
    title = f"{pair} — SMC/ICT Analysis"
    if data_note:
        title += f"<br><sup style='color:#8b949e'>{data_note}</sup>"
    fig.update_layout(
        title=title,
        xaxis_title="Time (UTC)",
        yaxis_title="Price",
        yaxis=dict(range=[y_min - pad, y_max + pad], autorange=False),
        template="plotly_dark",
        xaxis_rangeslider_visible=False,
        height=600,
        width=1200,
        margin=dict(l=50, r=160, t=60, b=50),
    )

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_pair = pair.replace("/", "-")
    file_path = f"{CHART_DIR}/chart_{safe_pair}_{ts}.png"

    try:
        fig.write_image(file_path, format="png")
        logger.info(f"[{datetime.now(timezone.utc)}] Chart saved: {file_path}")
        return file_path
    except Exception as e:
        logger.error(f"[{datetime.now(timezone.utc)}] Failed to save chart for {pair}: {e}")
        return None


def cleanup_old_charts() -> int:
    """Delete /tmp/chart_*.png files older than CHART_MAX_AGE_HOURS. Returns count deleted."""
    cutoff = time.time() - CHART_MAX_AGE_HOURS * 3600
    deleted = 0
    for path in glob.glob(f"{CHART_DIR}/chart_*.png"):
        if os.path.getmtime(path) < cutoff:
            try:
                os.remove(path)
                deleted += 1
            except OSError as e:
                logger.error(f"[{datetime.now(timezone.utc)}] Could not delete {path}: {e}")
    return deleted


def test_plotter():
    """Standalone test — generates a chart from synthetic XAUUSD data."""
    data = {
        "open": [2300, 2310, 2320, 2315, 2330, 2325, 2340, 2335, 2350, 2345],
        "high": [2315, 2325, 2330, 2335, 2345, 2340, 2355, 2350, 2360, 2355],
        "low": [2295, 2305, 2315, 2310, 2325, 2320, 2335, 2330, 2345, 2340],
        "close": [2310, 2320, 2315, 2330, 2325, 2340, 2335, 2350, 2345, 2352],
        "volume": [1000] * 10,
        "datetime": pd.date_range("2024-01-01", periods=10, freq="1h", tz="UTC"),
    }
    df = pd.DataFrame(data)
    mock_signal = {"direction": "LONG", "entry_low": 2318.0, "entry_high": 2322.0}
    path = generate_chart(df, "XAUUSD", [], [], {}, [], [], mock_signal)
    print(f"Chart saved: {path}")
    return path


if __name__ == "__main__":
    test_plotter()
