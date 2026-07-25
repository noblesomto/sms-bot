"""One-off script: records live candle fixtures for the golden capture harness.

Run once — BEFORE any refactor of the scan pipeline — to snapshot real
candles for 4 pairs x 3 timeframes (15min/1h/4h) so tests/golden/capture.py
can replay the pipeline deterministically without hitting the network.

yfinance + pyarrow are installed in this venv, so fixtures are recorded via
the primary yfinance path (matching production) and saved as parquet
(tests/golden/fixtures/{PAIR_SAFE}_{TF}.parquet), per the task brief.

Safe to rerun later if fixtures need refreshing — just make sure to
recapture tests/golden/expected.json from a known-good (already reviewed)
version of the pipeline afterward, never from a change under test.
"""
from pathlib import Path

from core.data_feed import get_candles

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

PAIRS = ["XAU/USD", "EUR/USD", "GBP/JPY", "US30"]
TIMEFRAMES = ["15min", "1h", "4h"]

PAIR_SAFE = {
    "XAU/USD": "XAU_USD",
    "EUR/USD": "EUR_USD",
    "GBP/JPY": "GBP_JPY",
    "US30": "US30",
}


def main():
    failures = []
    for pair in PAIRS:
        for tf in TIMEFRAMES:
            df = get_candles(pair, tf, 250, force_fresh=True)
            if df is None or df.empty:
                print(f"FAILED: {pair}/{tf}")
                failures.append((pair, tf))
                continue
            safe = PAIR_SAFE[pair]
            path = FIXTURES_DIR / f"{safe}_{tf}.parquet"
            df.to_parquet(path)
            print(f"Saved {pair}/{tf}: {len(df)} rows -> {path}")
    if failures:
        raise SystemExit(f"Failed to fetch: {failures}")


if __name__ == "__main__":
    main()
