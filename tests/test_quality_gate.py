"""Quality gate + hot-grab tightening tests."""

from __future__ import annotations

import pandas as pd

from quality_gate import (
    ema_stack_ok,
    evaluate_hot_grab,
    evaluate_quality,
    extension_ok,
    rsi_ok,
)


def _row(**kwargs) -> pd.Series:
    base = {
        "close": 1.10,
        "ema_20": 1.10,
        "ema_50": 1.09,
        "ema_200": 1.08,
        "rsi_14": 50.0,
        "atr_14": 0.0010,
        "ema_20_1h": 1.10,
        "ema_50_1h": 1.09,
    }
    base.update(kwargs)
    return pd.Series(base)


def test_ema_stack_buy_pass_fail():
    # close > ema20 > ema50 > ema200
    ok, _ = ema_stack_ok(
        _row(close=1.101, ema_20=1.100, ema_50=1.090, ema_200=1.080),
        "buy",
    )
    assert ok
    bad, _ = ema_stack_ok(_row(ema_20=1.07, ema_50=1.08, ema_200=1.09, close=1.06), "buy")
    assert not bad


def test_rsi_bands():
    ok, _ = rsi_ok(_row(rsi_14=50), "buy", __import__("config").QUALITY)
    assert ok
    bad, _ = rsi_ok(_row(rsi_14=80), "buy", __import__("config").QUALITY)
    assert not bad


def test_extension_rejects_chase():
    ok, _ = extension_ok(_row(close=1.10, ema_20=1.10, atr_14=0.001), "buy", __import__("config").QUALITY)
    assert ok
    # 3 ATR away
    far, msg = extension_ok(
        _row(close=1.1035, ema_20=1.10, atr_14=0.001),
        "buy",
        __import__("config").QUALITY,
    )
    assert not far
    assert "Extended" in msg


def test_hot_grab_requires_h1_or_non_opposing_policy():
    # Soft bullish + H1 bearish + policy sell → reject (extension kept tight)
    row = _row(
        close=1.1005,
        ema_20=1.1000,
        ema_50=1.0990,
        ema_200=1.0900,
        ema_20_1h=1.0800,
        ema_50_1h=1.0900,  # H1 bearish
        atr_14=0.0100,
        rsi_14=50,
    )
    gate = evaluate_hot_grab(
        "EURUSDm",
        row,
        "buy",
        scanner_score=0.95,
        policy_action=2,  # sell opposing
    )
    assert not gate.allowed
    assert "opposing" in gate.reject_reason.lower() or "H1" in gate.reject_reason

    # Same setup but policy HOLD (not opposing) → allow via policy-not-opposing path
    gate2 = evaluate_hot_grab(
        "EURUSDm",
        row,
        "buy",
        scanner_score=0.95,
        policy_action=0,
    )
    assert gate2.allowed, gate2.reject_reason


def test_evaluate_quality_rejects_low_score_setup():
    # Bearish stack for a buy request → reject
    row = _row(ema_20=1.07, ema_50=1.08, ema_200=1.09, close=1.06, rsi_14=50)
    gate = evaluate_quality("EURUSDm", row, "buy")
    assert not gate.allowed
