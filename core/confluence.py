import logging
from typing import Optional

logger = logging.getLogger(__name__)


def score_signal(
    direction: str,
    htf_bias: str,
    ob_at_price: Optional[dict],
    fvg_at_price: Optional[dict],
    liquidity_sweeps: list,
    in_kill_zone: bool,
    pd_zone: dict,
    ltf_choch: Optional[dict],
    wyckoff_context: Optional[dict] = None,
) -> dict:
    """
    Multi-factor SMC/ICT + Wyckoff confluence scorer.

    HTF bias alignment is NOT scored here — it's a mandatory hard gate
    (scheduler.py Filter 2 blocks any signal that conflicts with it), so
    scoring it too would double-count a guaranteed pass and inflate every
    surviving signal's score by 2 points for free. It's still recorded in
    `factors` for alert/dashboard transparency.

    Base SMC/ICT factors (max 8):
      Price at valid OB           +2
      FVG present in zone         +1
      Liquidity swept before entry+2
      In ICT kill zone            +1
      Correct premium/discount    +1
      CHoCH confirmed on LTF      +1

    Wyckoff bonus (score can exceed 8):
      Phase C Spring confirmed    +2
      Spring tested               +1  (additional)
      Spring at Bullish OB        +1  (additional)
      Phase C UTAD confirmed      +2
      UTAD tested                 +1  (additional)
      UTAD at Bearish OB          +1  (additional)

    Returns dict with score, direction, factors, and max_score.
    """
    score = 0
    max_score = 8
    factors: list[str] = []

    htf_expected = "BULLISH" if direction == "LONG" else "BEARISH"
    if htf_bias == htf_expected:
        factors.append(f"HTF {htf_bias} bias aligns with {direction} signal")

    if ob_at_price and not ob_at_price.get("mitigated", False):
        ob_dir = ob_at_price["direction"]
        if (direction == "LONG" and ob_dir == "BULLISH") or (
            direction == "SHORT" and ob_dir == "BEARISH"
        ):
            score += 2
            label = "Bullish" if ob_dir == "BULLISH" else "Bearish"
            factors.append(
                f"Price at {label} OB ({ob_at_price['ob_low']} – {ob_at_price['ob_high']})"
            )

    fvg_aligned = False
    if fvg_at_price and fvg_at_price.get("status") in ("FRESH", "PARTIALLY_FILLED"):
        fvg_dir = fvg_at_price.get("direction", "")
        expected_fvg = "BULLISH" if direction == "LONG" else "BEARISH"
        fvg_aligned = (fvg_dir == expected_fvg)
        score += 1
        factors.append("FVG present in zone")

    # OB + FVG confluence zone — both at current price, directionally aligned
    if ob_at_price and not ob_at_price.get("mitigated") and fvg_aligned:
        ob_dir = ob_at_price.get("direction", "")
        ob_aligned_dir = (direction == "LONG" and ob_dir == "BULLISH") or \
                         (direction == "SHORT" and ob_dir == "BEARISH")
        if ob_aligned_dir:
            score += 1
            max_score += 1
            factors.append("OB+FVG confluence zone")

    relevant_sweeps = [
        s for s in liquidity_sweeps
        if (direction == "LONG" and s["type"] == "SELL_SIDE_SWEPT")
        or (direction == "SHORT" and s["type"] == "BUY_SIDE_SWEPT")
    ]
    if relevant_sweeps:
        score += 2
        side = "Sell-side" if direction == "LONG" else "Buy-side"
        factors.append(f"{side} liquidity swept")

    if in_kill_zone:
        score += 1
        factors.append("In kill zone")

    zone = pd_zone.get("zone", "EQUILIBRIUM")
    if (direction == "LONG" and zone == "DISCOUNT") or (
        direction == "SHORT" and zone == "PREMIUM"
    ):
        score += 1
        label = "Discount" if zone == "DISCOUNT" else "Premium"
        side_label = "below" if zone == "DISCOUNT" else "above"
        factors.append(f"{label} zone ({side_label} 50% EQ)")

    if ltf_choch:
        choch_dir = ltf_choch.get("direction")
        if (direction == "LONG" and choch_dir == "BULLISH") or (
            direction == "SHORT" and choch_dir == "BEARISH"
        ):
            score += 1
            factors.append("CHoCH confirmed on LTF")

    # ── Wyckoff Phase C bonus (MEDIUM/HIGH confidence only) ───────────────
    if wyckoff_context and wyckoff_context.get("confidence") in ("MEDIUM", "HIGH"):
        wyckoff_bias = wyckoff_context.get("bias")
        key_event = wyckoff_context.get("key_event", "")
        ob_conf = wyckoff_context.get("ob_confluence", False)

        if direction == "LONG" and wyckoff_bias == "BULLISH":
            max_score += 2
            score += 2
            factors.append(f"Wyckoff Accumulation Phase C — Spring confirmed")
            if "TESTED" in key_event:
                max_score += 1
                score += 1
                factors.append("Spring tested (no supply)")
            if ob_conf:
                max_score += 1
                score += 1
                factors.append("Spring at Bullish OB — ICT+Wyckoff confluence")

        elif direction == "SHORT" and wyckoff_bias == "BEARISH":
            max_score += 2
            score += 2
            factors.append(f"Wyckoff Distribution Phase C — UTAD confirmed")
            if "TESTED" in key_event:
                max_score += 1
                score += 1
                factors.append("UTAD tested (no demand)")
            if ob_conf:
                max_score += 1
                score += 1
                factors.append("UTAD at Bearish OB — ICT+Wyckoff confluence")

    return {"score": score, "direction": direction, "factors": factors, "max_score": max_score}


def test_confluence():
    """Standalone test with a fully-confluent LONG scenario."""
    mock_ob = {"direction": "BULLISH", "ob_low": 2318.40, "ob_high": 2321.80, "mitigated": False}
    mock_fvg = {"direction": "BULLISH", "fvg_low": 2319.0, "fvg_high": 2320.5, "status": "FRESH"}
    mock_sweeps = [{"type": "SELL_SIDE_SWEPT", "level": 2310.0, "direction": "BULLISH"}]
    mock_pd = {"zone": "DISCOUNT", "equilibrium": 2340.0}
    mock_choch = {"direction": "BULLISH", "type": "CHoCH"}

    result = score_signal(
        direction="LONG",
        htf_bias="BULLISH",
        ob_at_price=mock_ob,
        fvg_at_price=mock_fvg,
        liquidity_sweeps=mock_sweeps,
        in_kill_zone=True,
        pd_zone=mock_pd,
        ltf_choch=mock_choch,
    )

    print(f"Confluence Score: {result['score']}/{result['max_score']}")
    for f in result["factors"]:
        print(f"  ✅ {f}")
    return result


if __name__ == "__main__":
    test_confluence()
