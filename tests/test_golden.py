"""Golden parity gate for the core/strategy.py:evaluate() extraction.

Reruns the exact same capture harness (tests/golden/capture.py) that was
used, unchanged, to produce tests/golden/expected.json against the
pre-refactor scheduler.py. If this test passes, the extraction of the
analysis-and-decision block into core.strategy.evaluate() produced
byte-for-byte identical scan_pair_timeframe() output (result dicts +
persisted Signal rows) — proving zero live behavior change.
"""
import json
from pathlib import Path

from tests.golden.capture import capture

EXPECTED_PATH = Path(__file__).parent / "golden" / "expected.json"


def test_golden_parity():
    expected = json.loads(EXPECTED_PATH.read_text())
    actual = capture()
    # actual isn't run through json dump/load, but capture() output is already
    # JSON-safe (plain dict/list/str/float/None) except datetimes in nested
    # analysis structures never make it into "runs"/"signals" — round-trip
    # through json to normalize types (e.g. tuples -> lists) exactly like
    # the frozen expected.json was written.
    actual = json.loads(json.dumps(actual, default=str))
    assert actual == expected
