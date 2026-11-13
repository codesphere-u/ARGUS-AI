"""
Comprehensive unit and integration tests for the ARGUS AI Deterministic Backtesting,
Point-in-Time Leakage Guard, Execution Cost Model, and Trade Passport Replay Engine.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from argus_ai.backtest import (
    AdversarialBacktestEngine,
    ExecutionCostModel,
    LookaheadLeakageError,
    PointInTimeGuard,
    compute_disagreement_entropy,
    compute_passport_digest,
    generate_multi_regime_dataset,
    replay_passport,
)
from argus_ai.cli import main as cli_main
from argus_ai.core.models import (
    AgentVote,
    ConsensusDecision,
    DevilsAdvocateReview,
    TradePassport,
)


@pytest.fixture(autouse=True)
def mock_llm_offline():
    """Ensure deterministic offline execution for all tests."""
    with patch("argus_ai.agents.base_agent.BaseAgent._initialize_gemini", return_value=None), patch(
        "argus_ai.agents.base_agent.BaseAgent.query_llm_json", return_value=None
    ):
        yield


def _sample_passport(status: str = "APPROVED", veto: bool = False, da_risk: float = 22.0) -> TradePassport:
    downsize = 0.0 if status == "VETOED" else (0.5 if status == "DOWNSIZED" else 1.0)
    return TradePassport(
        passport_id="PASSPORT-20260105-TEST01",
        timestamp="2026-01-05T14:00:00+00:00",
        ticker="NVDA",
        asset_class="US_EQUITY",
        direction="LONG",
        current_price=140.0,
        target_entry=140.0,
        stop_loss=137.0,
        take_profit=146.0,
        risk_reward_ratio=2.0,
        shares_or_contracts=300.0 * downsize,
        position_size_usd=42000.0 * downsize,
        account_risk_usd=900.0 * downsize,
        account_risk_pct=0.90 * downsize,
        consensus=ConsensusDecision(
            status=status,
            overall_confidence=0.82,
            composite_score=54.0,
            downsize_factor=downsize,
            primary_catalyst="Bullish EMA alignment",
            main_opposition="Minor valuation stretch",
            executive_summary="Approved with strong consensus",
            unanimous=True,
        ),
        agent_votes=[
            AgentVote(agent_name="Quant Agent", recommendation="BUY", confidence=0.85, score=60.0),
            AgentVote(agent_name="News & Sentiment Agent", recommendation="BUY", confidence=0.78, score=50.0),
            AgentVote(agent_name="Macro & Sector Agent", recommendation="BUY", confidence=0.80, score=55.0),
            AgentVote(agent_name="Options Flow Agent", recommendation="BUY", confidence=0.80, score=45.0),
        ],
        devils_advocate=DevilsAdvocateReview(
            adversarial_thesis="Standard risk parameters observed.",
            identified_traps=["No major structural bear/bull traps identified"],
            disqualification_risk_pct=da_risk,
            veto_recommended=veto,
            recommended_contingency="Standard 1.5x ATR stop",
            severity_level="LOW" if da_risk < 45 else ("MODERATE" if da_risk < 75 else "CRITICAL"),
        ),
        execution_status="SIMULATED",
    )


def test_point_in_time_guard_accepts_valid_slice():
    df = generate_multi_regime_dataset("NVDA", n_bars=80, seed=7)
    decision_ts = df.index[49]
    sliced = df.iloc[:50]
    validated = PointInTimeGuard.validate_slice(sliced, decision_ts)
    assert len(validated) == 50
    assert validated.index[-1] == decision_ts


def test_point_in_time_guard_rejects_future_lookahead_bar():
    df = generate_multi_regime_dataset("NVDA", n_bars=80, seed=7)
    decision_ts = df.index[40]
    leaked_slice = df.iloc[:45]  # Contains 4 bars strictly after decision_ts
    with pytest.raises(LookaheadLeakageError, match="Point-in-time leakage detected"):
        PointInTimeGuard.validate_slice(leaked_slice, decision_ts)


def test_point_in_time_guard_rejects_non_monotonic_timestamps():
    df = generate_multi_regime_dataset("NVDA", n_bars=80, seed=7).iloc[:20].copy()
    shuffled = df.iloc[[0, 2, 1, 3, 4]]
    with pytest.raises(LookaheadLeakageError, match="monotonically increasing"):
        PointInTimeGuard.validate_slice(shuffled, df.index[10])


def test_point_in_time_guard_rejects_empty_dataframe():
    empty_df = pd.DataFrame()
    with pytest.raises(LookaheadLeakageError, match="empty"):
        PointInTimeGuard.validate_slice(empty_df, "2026-01-05T12:00:00Z")


def test_execution_cost_model_adverse_fills_and_commissions():
    cost_model = ExecutionCostModel(spread_bps=6.0, commission_bps=2.0, slippage_atr_fraction=0.05)
    raw_price = 100.0
    atr = 2.0

    # Buying LONG entry pays half-spread (0.03) + 5% of ATR (0.10) = 100.13
    long_entry = cost_model.compute_fill_price(raw_price, "LONG", atr, is_entry=True)
    assert long_entry == pytest.approx(100.13, abs=1e-4)

    # Selling LONG exit receives 100.0 - 0.13 = 99.87
    long_exit = cost_model.compute_fill_price(raw_price, "LONG", atr, is_entry=False)
    assert long_exit == pytest.approx(99.87, abs=1e-4)

    # Short entry sells at 99.87, Short exit buys to cover at 100.13
    short_entry = cost_model.compute_fill_price(raw_price, "SHORT", atr, is_entry=True)
    short_exit = cost_model.compute_fill_price(raw_price, "SHORT", atr, is_entry=False)
    assert short_entry == pytest.approx(99.87, abs=1e-4)
    assert short_exit == pytest.approx(100.13, abs=1e-4)

    rt_cost = cost_model.compute_round_trip_cost(100.0, 104.0, shares=50.0, atr=2.0, direction="LONG")
    assert rt_cost > 14.0


def test_disagreement_entropy_unanimous_vs_divided_committee():
    unanimous_votes = [
        AgentVote(agent_name="A1", recommendation="BUY", confidence=0.85, score=60.0),
        AgentVote(agent_name="A2", recommendation="BUY", confidence=0.80, score=50.0),
        AgentVote(agent_name="A3", recommendation="BUY", confidence=0.75, score=45.0),
        AgentVote(agent_name="A4", recommendation="BUY", confidence=0.90, score=70.0),
    ]
    assert compute_disagreement_entropy(unanimous_votes) == 0.0

    split_votes = [
        AgentVote(agent_name="A1", recommendation="BUY", confidence=0.80, score=55.0),
        AgentVote(agent_name="A2", recommendation="SELL", confidence=0.80, score=-55.0),
        AgentVote(agent_name="A3", recommendation="HOLD", confidence=0.80, score=0.0),
        AgentVote(agent_name="A4", recommendation="VETO", confidence=0.80, score=-90.0),
    ]
    assert compute_disagreement_entropy(split_votes) == pytest.approx(1.0, abs=1e-3)


def test_passport_digest_determinism_and_tamper_detection():
    p1 = _sample_passport()
    p2 = _sample_passport()
    d1 = compute_passport_digest(p1)
    d2 = compute_passport_digest(p2)
    assert d1 == d2
    assert len(d1) == 64

    # Tamper with stop_loss
    p2.stop_loss = 135.5
    assert compute_passport_digest(p2) != d1


def test_replay_passport_validates_authentic_passport(tmp_path: Path):
    p = _sample_passport()
    digest = compute_passport_digest(p)
    passport_file = tmp_path / "passport.json"
    passport_file.write_text(p.model_dump_json(indent=2), encoding="utf-8")

    report = replay_passport(passport_file, expected_digest=digest)
    assert report.is_valid is True
    assert report.expected_digest_matched is True
    assert report.violations == []
    assert "DEVILS_ADVOCATE_VETO_AUTHORITY" in report.invariants_checked


def test_replay_passport_catches_veto_bypass_violation():
    # Devil's Advocate recommends VETO (88% risk), but consensus claims APPROVED
    bad_passport = _sample_passport(status="APPROVED", veto=True, da_risk=88.0)
    report = replay_passport(bad_passport)
    assert report.is_valid is False
    assert any("VETO breach" in v for v in report.violations)


def test_replay_passport_catches_bracket_and_digest_violations():
    p = _sample_passport()
    valid_digest = compute_passport_digest(p)
    # Invert stop loss above entry on a LONG trade
    p.stop_loss = 142.0
    p.risk_reward_ratio = 1.1
    report = replay_passport(p.model_dump(), expected_digest=valid_digest)
    assert report.is_valid is False
    assert report.expected_digest_matched is False
    assert len(report.violations) >= 3


def test_adversarial_backtest_engine_outperforms_naive_baseline():
    df = generate_multi_regime_dataset("NVDA", n_bars=240, seed=42)
    engine = AdversarialBacktestEngine()
    comparison = engine.run_backtest(df, ticker="NVDA")

    argus = comparison.argus_committee
    naive = comparison.naive_baseline

    assert comparison.evaluated_opportunities >= 40
    assert argus.total_return_pct > naive.total_return_pct
    assert argus.max_drawdown_pct < naive.max_drawdown_pct
    assert argus.sharpe_ratio > naive.sharpe_ratio
    assert argus.vetoed_trades_count >= 2
    assert argus.downsized_trades_count >= 1
    assert argus.veto_saved_loss_usd > 0.0
    assert argus.veto_accuracy_pct >= 50.0
    assert argus.total_costs_usd > 0.0

    md = AdversarialBacktestEngine.to_markdown_report(comparison)
    assert "ARGUS 7-Agent Committee" in md
    assert "Veto Saved Loss" in md


def test_cli_backtest_and_replay_commands(tmp_path: Path):
    report_out = tmp_path / "bt_report.md"
    cli_main(["backtest", "--ticker", "NVDA", "--bars", "140", "--report-out", str(report_out)])
    assert report_out.exists()
    assert "Counterfactual Benchmark" in report_out.read_text(encoding="utf-8")

    passport_path = tmp_path / "sample_passport.json"
    passport_path.write_text(_sample_passport().model_dump_json(), encoding="utf-8")
    cli_main(["replay", "--passport", str(passport_path)])


def test_export_veto_chart_requires_matplotlib_with_helpful_error(monkeypatch, tmp_path: Path):
    import builtins
    real_import = builtins.__import__

    def _block_matplotlib(name, *args, **kwargs):
        if name.startswith("matplotlib"):
            raise ImportError("No module named 'matplotlib'")
        return real_import(name, *args, **kwargs)

    df = generate_multi_regime_dataset("NVDA", n_bars=120, seed=42)
    comparison = AdversarialBacktestEngine().run_backtest(df, ticker="NVDA")
    monkeypatch.setattr(builtins, "__import__", _block_matplotlib)

    import pytest
    with pytest.raises(ImportError, match=r"argus-ai\[rl\]"):
        AdversarialBacktestEngine.export_veto_chart(comparison, str(tmp_path / "chart.svg"))
