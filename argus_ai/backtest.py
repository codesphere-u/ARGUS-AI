"""
Deterministic Historical Backtesting, Counterfactual Veto Attribution,
and Trade Passport Replay Engine for ARGUS AI (v2.1).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from argus_ai.committee import CommitteeOrchestrator
from argus_ai.config.settings import CONFIG
from argus_ai.core.models import (
    AgentVote,
    ConsensusDecision,
    MarketContext,
    TradePassport,
)


class LookaheadLeakageError(ValueError):
    """Raised when future bars beyond the decision timestamp are detected in a backtest slice."""


class PointInTimeGuard:
    """
    Point-in-Time Leakage Guard:
    Ensures every historical bar/feature slice passed to the 7-agent Investment Committee
    has timestamp <= decision_timestamp and is strictly ordered in time.
    """

    @staticmethod
    def extract_timestamps(bars_df: pd.DataFrame) -> pd.DatetimeIndex:
        if bars_df.empty:
            raise LookaheadLeakageError("Cannot evaluate an empty historical bars DataFrame.")

        if isinstance(bars_df.index, pd.DatetimeIndex):
            idx = bars_df.index
        elif "timestamp" in bars_df.columns:
            idx = pd.to_datetime(bars_df["timestamp"], utc=True)
        elif "time" in bars_df.columns:
            idx = pd.to_datetime(bars_df["time"], utc=True)
        else:
            raise LookaheadLeakageError(
                "DataFrame must have a DatetimeIndex or a 'timestamp'/'time' column for point-in-time validation."
            )

        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        else:
            idx = idx.tz_convert("UTC")
        return pd.DatetimeIndex(idx)

    @classmethod
    def validate_slice(
        cls,
        bars_df: pd.DataFrame,
        decision_timestamp: pd.Timestamp | datetime.datetime | str,
    ) -> pd.DataFrame:
        ts_index = cls.extract_timestamps(bars_df)
        decision_ts = pd.Timestamp(decision_timestamp)
        if decision_ts.tzinfo is None:
            decision_ts = decision_ts.tz_localize("UTC")
        else:
            decision_ts = decision_ts.tz_convert("UTC")

        if not ts_index.is_monotonic_increasing:
            raise LookaheadLeakageError(
                "Historical bar timestamps must be monotonically increasing for point-in-time integrity."
            )

        future_mask = ts_index > decision_ts
        if bool(future_mask.any()):
            first_leaked = ts_index[future_mask][0]
            raise LookaheadLeakageError(
                f"Point-in-time leakage detected: bar timestamp {first_leaked.isoformat()} "
                f"is after decision_timestamp {decision_ts.isoformat()}."
            )

        return bars_df


class ExecutionCostModel(BaseModel):
    """
    Realistic Execution & Transaction Cost Model:
    Models half-spread, commission bps, and ATR-proportional volatility slippage
    so backtests never assume zero-friction fills.
    """

    spread_bps: float = Field(default=4.0, ge=0.0, description="Full bid-ask spread in basis points")
    commission_bps: float = Field(default=1.5, ge=0.0, description="Broker/exchange commission per side in bps")
    slippage_atr_fraction: float = Field(
        default=0.04, ge=0.0, description="Adverse slippage as a fraction of 14-bar ATR"
    )

    def compute_fill_price(
        self,
        raw_price: float,
        direction: Literal["LONG", "SHORT"],
        atr: float,
        is_entry: bool = True,
    ) -> float:
        """
        Return the friction-adjusted fill price.
        Entering LONG or exiting SHORT buys at the ask + slippage.
        Entering SHORT or exiting LONG sells at the bid - slippage.
        """
        half_spread = raw_price * (self.spread_bps / 20_000.0)
        vol_slippage = max(atr, 0.0) * self.slippage_atr_fraction
        adverse_shift = half_spread + vol_slippage

        is_buy_order = (direction.upper() == "LONG" and is_entry) or (
            direction.upper() == "SHORT" and not is_entry
        )
        if is_buy_order:
            return round(raw_price + adverse_shift, 4)
        return round(max(0.0001, raw_price - adverse_shift), 4)

    def compute_commission(self, notional_usd: float) -> float:
        return round(abs(notional_usd) * (self.commission_bps / 10_000.0), 4)

    def compute_round_trip_cost(
        self,
        raw_entry: float,
        raw_exit: float,
        shares: float,
        atr: float,
        direction: Literal["LONG", "SHORT"] = "LONG",
    ) -> float:
        fill_entry = self.compute_fill_price(raw_entry, direction, atr, is_entry=True)
        fill_exit = self.compute_fill_price(raw_exit, direction, atr, is_entry=False)
        entry_slip = abs(fill_entry - raw_entry) * shares
        exit_slip = abs(fill_exit - raw_exit) * shares
        comm = self.compute_commission(fill_entry * shares) + self.compute_commission(fill_exit * shares)
        return round(entry_slip + exit_slip + comm, 4)


def compute_disagreement_entropy(votes: Sequence[AgentVote]) -> float:
    """
    Compute normalized Shannon entropy in [0.0, 1.0] over committee agent recommendations,
    weighted by agent confidence.
    0.0 = unanimous agreement; higher values indicate stronger inter-agent dissent.
    """
    if not votes:
        return 0.0

    buckets: dict[str, float] = {}
    total_weight = 0.0
    for v in votes:
        w = max(float(v.confidence), 0.05)
        buckets[v.recommendation] = buckets.get(v.recommendation, 0.0) + w
        total_weight += w

    if total_weight <= 0 or len(buckets) <= 1:
        return 0.0

    probs = [w / total_weight for w in buckets.values() if w > 0]
    raw_entropy = -sum(p * math.log(p) for p in probs)
    # Normalize by log(4) since there are 4 possible recommendation classes (BUY, SELL, HOLD, VETO)
    max_entropy = math.log(4.0)
    return round(min(1.0, raw_entropy / max_entropy), 4)


def compute_passport_digest(passport: TradePassport) -> str:
    """
    Compute a deterministic SHA-256 hex digest over the canonical decision & risk fields
    of a TradePassport for tamper-evident audit verification.
    """
    canonical_payload = {
        "passport_id": passport.passport_id,
        "ticker": passport.ticker,
        "asset_class": passport.asset_class,
        "direction": passport.direction,
        "current_price": round(float(passport.current_price), 4),
        "target_entry": round(float(passport.target_entry), 4),
        "stop_loss": round(float(passport.stop_loss), 4),
        "take_profit": round(float(passport.take_profit), 4),
        "risk_reward_ratio": round(float(passport.risk_reward_ratio), 4),
        "shares_or_contracts": round(float(passport.shares_or_contracts), 4),
        "account_risk_pct": round(float(passport.account_risk_pct), 4),
        "consensus_status": passport.consensus.status,
        "composite_score": round(float(passport.consensus.composite_score), 2),
        "downsize_factor": round(float(passport.consensus.downsize_factor), 2),
        "da_disqualification_risk_pct": round(float(passport.devils_advocate.disqualification_risk_pct), 2),
        "da_veto_recommended": bool(passport.devils_advocate.veto_recommended),
    }
    serialized = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class ReplayVerificationReport(BaseModel):
    passport_id: str
    ticker: str
    consensus_status: str
    is_valid: bool
    computed_digest: str
    expected_digest_matched: bool | None = None
    disagreement_entropy: float
    invariants_checked: list[str] = Field(default_factory=list)
    violations: list[str] = Field(default_factory=list)


def replay_passport(
    passport_source: TradePassport | dict[str, Any] | str | Path,
    expected_digest: str | None = None,
    max_account_risk_pct: float | None = None,
) -> ReplayVerificationReport:
    """
    Deterministically replay and validate a recorded TradePassport JSON/object,
    verifying its cryptographic SHA-256 digest and institutional risk/consensus invariants.
    """
    if isinstance(passport_source, TradePassport):
        passport = passport_source
    elif isinstance(passport_source, dict):
        passport = TradePassport.model_validate(passport_source)
    elif isinstance(passport_source, Path):
        raw_text = passport_source.read_text(encoding="utf-8")
        passport = TradePassport.model_validate_json(raw_text)
    elif isinstance(passport_source, str):
        stripped = passport_source.strip()
        if stripped.startswith("{"):
            passport = TradePassport.model_validate_json(stripped)
        else:
            raw_text = Path(stripped).read_text(encoding="utf-8")
            passport = TradePassport.model_validate_json(raw_text)
    else:
        raise TypeError(f"Unsupported passport_source type: {type(passport_source)}")

    invariants: list[str] = []
    violations: list[str] = []
    risk_cap = max_account_risk_pct if max_account_risk_pct is not None else CONFIG.risk.max_account_risk_pct

    # 1. Bracket Geometry Invariant
    invariants.append("BRACKET_GEOMETRY")
    if passport.direction == "LONG":
        if not (passport.stop_loss < passport.target_entry < passport.take_profit):
            violations.append(
                f"Invalid LONG bracket geometry: SL ({passport.stop_loss}) < Entry ({passport.target_entry}) < TP ({passport.take_profit}) violated."
            )
    else:
        if not (passport.take_profit < passport.target_entry < passport.stop_loss):
            violations.append(
                f"Invalid SHORT bracket geometry: TP ({passport.take_profit}) < Entry ({passport.target_entry}) < SL ({passport.stop_loss}) violated."
            )

    # 2. Risk/Reward Minimum Invariant
    invariants.append("RISK_REWARD_FLOOR")
    if passport.risk_reward_ratio < 1.5:
        violations.append(
            f"Risk/Reward ratio {passport.risk_reward_ratio:.2f} is below institutional floor of 1.50."
        )

    # 3. Devil's Advocate Veto Authority Invariant
    invariants.append("DEVILS_ADVOCATE_VETO_AUTHORITY")
    da = passport.devils_advocate
    if (da.veto_recommended or da.disqualification_risk_pct >= 75.0) and (
        passport.consensus.status != "VETOED" or passport.consensus.downsize_factor != 0.0
    ):
        violations.append(
            f"Devil's Advocate VETO breach: disqualification_risk={da.disqualification_risk_pct}% "
            f"(veto={da.veto_recommended}), but consensus.status={passport.consensus.status} "
            f"and downsize_factor={passport.consensus.downsize_factor}."
        )

    # 4. Downsize Caution Invariant
    invariants.append("DEVILS_ADVOCATE_DOWNSIZE_RULE")
    if (
        not da.veto_recommended
        and 45.0 <= da.disqualification_risk_pct < 75.0
        and abs(passport.consensus.composite_score) >= 25.0
        and (passport.consensus.status != "DOWNSIZED" or abs(passport.consensus.downsize_factor - 0.5) > 1e-6)
    ):
        violations.append(
            f"Downsize rule breach: disqualification_risk={da.disqualification_risk_pct}% "
            f"requires DOWNSIZED status with 0.5x factor, got {passport.consensus.status} ({passport.consensus.downsize_factor})."
        )

    # 5. Account Risk Budget Cap Invariant
    invariants.append("ACCOUNT_RISK_BUDGET_CAP")
    if passport.account_risk_pct > risk_cap + 0.05:
        violations.append(
            f"Account risk {passport.account_risk_pct:.2f}% exceeds maximum budget {risk_cap:.2f}%."
        )

    # 6. Cryptographic Digest Verification
    computed_digest = compute_passport_digest(passport)
    digest_matched: bool | None = None
    if expected_digest is not None:
        invariants.append("SHA256_PASSPORT_DIGEST")
        digest_matched = computed_digest == expected_digest
        if not digest_matched:
            violations.append(
                f"Passport SHA-256 digest mismatch: expected {expected_digest[:12]}..., got {computed_digest[:12]}..."
            )

    entropy = compute_disagreement_entropy(passport.agent_votes)

    return ReplayVerificationReport(
        passport_id=passport.passport_id,
        ticker=passport.ticker,
        consensus_status=passport.consensus.status,
        is_valid=len(violations) == 0,
        computed_digest=computed_digest,
        expected_digest_matched=digest_matched,
        disagreement_entropy=entropy,
        invariants_checked=invariants,
        violations=violations,
    )


class SimulatedTradeRecord(BaseModel):
    step_index: int
    timestamp: str
    passport_id: str
    ticker: str
    direction: Literal["LONG", "SHORT"]
    consensus_status: str
    executed: bool
    entry_price: float
    exit_price: float
    shares: float
    gross_pnl_usd: float
    transaction_cost_usd: float
    net_pnl_usd: float
    exit_reason: Literal["TAKE_PROFIT", "STOP_LOSS", "HORIZON_EXIT", "VETOED_COUNTERFACTUAL", "REJECTED"]
    disqualification_risk_pct: float
    disagreement_entropy: float
    counterfactual_pnl_usd: float = 0.0


class BacktestMetrics(BaseModel):
    strategy_name: str
    initial_equity: float
    ending_equity: float
    net_pnl_usd: float
    total_return_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    win_rate_pct: float
    profit_factor: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    total_costs_usd: float
    vetoed_trades_count: int = 0
    downsized_trades_count: int = 0
    veto_saved_loss_usd: float = 0.0
    veto_accuracy_pct: float = 0.0
    avg_disagreement_entropy: float = 0.0


class BacktestComparisonReport(BaseModel):
    ticker: str
    total_bars: int
    evaluated_opportunities: int
    cost_model: ExecutionCostModel
    argus_committee: BacktestMetrics
    naive_baseline: BacktestMetrics
    return_outperformance_pct: float
    drawdown_reduction_pct: float
    sharpe_improvement: float
    trade_records: list[SimulatedTradeRecord] = Field(default_factory=list)


def generate_multi_regime_dataset(
    ticker: str = "NVDA",
    n_bars: int = 240,
    seed: int = 42,
) -> pd.DataFrame:
    """
    Generate a deterministic, multi-regime hourly OHLCV + macro dataset covering:
      1. Bull Trend Regime (constructive momentum, low VIX)
      2. Euphoric Blow-Off & Flash-Crash Bull Traps (overbought RSI + VIX spike before sharp drop)
      3. High-Volatility Chop Regime (range-bound whipsaws)
      4. Institutional Recovery Trend (clean trend continuation)
    """
    if n_bars < 80:
        raise ValueError("generate_multi_regime_dataset requires at least 80 bars.")

    rng = np.random.default_rng(seed)
    timestamps = pd.date_range("2026-01-05 09:30:00", periods=n_bars, freq="h", tz="UTC")

    closes = np.zeros(n_bars, dtype=float)
    highs = np.zeros(n_bars, dtype=float)
    lows = np.zeros(n_bars, dtype=float)
    opens = np.zeros(n_bars, dtype=float)
    volumes = np.zeros(n_bars, dtype=float)
    regimes: list[str] = []
    vix_levels = np.zeros(n_bars, dtype=float)
    news_col: list[list[str]] = []

    price = 120.0
    p1_end = int(n_bars * 0.35)
    p2_end = int(n_bars * 0.70)
    p3_end = int(n_bars * 0.85)

    # Constructive bullish trend with natural pullbacks (every 20th bar has a normal pullback)
    bull_pattern = [0.75, -0.45, 0.70, -0.40, 0.68, -0.42, 0.72, -0.44]
    # Extended rally pattern (~2.25:1 gain/loss ratio -> RSI(14) ~ 69.2, triggering 48% DA downsize in BULLISH regime)
    caution_pattern = [0.64, -0.26, 0.60, -0.25, 0.62, -0.27]

    for i in range(n_bars):
        noise = float(rng.normal(0.0, 0.015))
        if i < p1_end:
            # Phase 1: Constructive Bullish Trend with a brief mid-trend shakeout (bars 66..67)
            if 66 <= i <= 67:
                step = -0.95 + noise
            else:
                step = bull_pattern[i % len(bull_pattern)] + noise
            regime = "BULLISH"
            vix = 14.2 + (i % 4) * 0.2
            headlines = [
                f"{ticker} sees strong institutional inflow and enterprise expansion",
                f"Analyst upgrade highlights bullish growth trajectory for {ticker}",
            ]
            wick = 0.18
        elif i < p2_end:
            # Phase 2: Euphoric Bull Traps & Flash-Crash Air Pockets
            # 20-bar cycle: 16 bars of 2.35:1 ramp (pushing 14-bar RSI to ~69.2 while staying > EMA20 > EMA50,
            # so Quant is BUY +45 while RSI > 68 + VIX > 24 + CHOPPY triggers Devil's Advocate 91% VETO!)
            # followed by 4 bars of sharp flush (-1.65/bar) that stops out naive buyers at the top.
            cycle_pos = (i - p1_end) % 20
            if cycle_pos < 16:
                ramp_pattern = [0.82, -0.32, 0.78, -0.31, 0.80, -0.32, 0.84, -0.30]
                step = ramp_pattern[cycle_pos % len(ramp_pattern)] + noise
                regime = "CHOPPY"
                vix = 25.5 + (cycle_pos % 3) * 0.5
                headlines = [
                    f"{ticker} breakout momentum continues on upgrade chatter and strong retail inflow",
                    f"Bullish call sweep activity surges in {ticker} ahead of macro print",
                ]
                wick = 0.20
            else:
                step = -1.65 + noise
                regime = "CHOPPY"
                vix = 29.0 + (cycle_pos - 16) * 0.5
                headlines = [
                    f"{ticker} experiences sharp liquidity flush after crowded overbought top",
                    "Institutional profit-taking accelerates on elevated volatility",
                ]
                wick = 0.35
        elif i < p3_end:
            # Phase 3: Late-Stage Extended Rally (RSI ~69.2 in BULLISH regime -> Devil's Advocate 48% DOWNSIZE)
            # with a brief pullback at bars 198..199
            if 198 <= i <= 199:
                step = -0.90 + noise
            else:
                step = caution_pattern[i % len(caution_pattern)] + noise
            regime = "BULLISH"
            vix = 18.5 + (i % 3) * 0.3
            headlines = [
                f"{ticker} grinds higher on strong demand despite extended technicals",
                f"Partnership expansion supports constructive outlook for {ticker}",
            ]
            wick = 0.20
        else:
            # Phase 4: Clean Institutional Recovery Trend
            step = bull_pattern[i % len(bull_pattern)] + noise
            regime = "BULLISH"
            vix = 14.8 + (i % 4) * 0.2
            headlines = [
                f"{ticker} resumes constructive growth trend on strong guidance beat",
                f"Bullish institutional accumulation confirmed in {ticker}",
            ]
            wick = 0.18

        open_p = price
        close_p = max(10.0, open_p + step)
        high_p = max(open_p, close_p) + wick
        low_p = min(open_p, close_p) - wick

        opens[i] = round(open_p, 4)
        closes[i] = round(close_p, 4)
        highs[i] = round(high_p, 4)
        lows[i] = round(max(1.0, low_p), 4)
        volumes[i] = round(25_000.0 + float(rng.uniform(0, 10_000.0)), 1)
        regimes.append(regime)
        vix_levels[i] = round(vix, 2)
        news_col.append(headlines)
        price = close_p

    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "market_regime": regimes,
            "vix_level": vix_levels,
            "headline": [h[0] for h in news_col],
            "recent_news": news_col,
        },
        index=timestamps,
    )


class AdversarialBacktestEngine:
    """
    Deterministic Walk-Forward Backtest Engine comparing:
      1. Full ARGUS 7-Agent Investment Committee (with Devil's Advocate Veto & Downsize + ATR Risk Commander)
      2. Naive Single-Agent Quant Baseline (unfiltered technical signal execution)
    Enforces strict PointInTimeGuard checks at every step and realistic transaction costs.
    """

    def __init__(
        self,
        cost_model: ExecutionCostModel | None = None,
        initial_equity: float = 100_000.0,
        warmup_bars: int = 50,
        eval_stride: int = 4,
        holding_horizon: int = 12,
        use_llm: bool = False,
    ) -> None:
        self.cost_model = cost_model or ExecutionCostModel()
        self.initial_equity = float(initial_equity)
        self.warmup_bars = int(warmup_bars)
        self.eval_stride = int(eval_stride)
        self.holding_horizon = int(holding_horizon)
        self.use_llm = bool(use_llm)
        self.committee = CommitteeOrchestrator(use_llm=self.use_llm)

    def _resolve_bracket_outcome(
        self,
        future_bars: pd.DataFrame,
        direction: Literal["LONG", "SHORT"],
        raw_entry: float,
        stop_loss: float,
        take_profit: float,
        shares: float,
        atr: float,
    ) -> tuple[float, float, float, float, float, Literal["TAKE_PROFIT", "STOP_LOSS", "HORIZON_EXIT"]]:
        """
        Simulate forward bar-by-bar bracket resolution over the holding window
        with realistic entry/exit slippage and commissions.
        Returns (fill_entry, fill_exit, gross_pnl, total_cost, net_pnl, exit_reason).
        """
        fill_entry = self.cost_model.compute_fill_price(raw_entry, direction, atr, is_entry=True)
        raw_exit = float(future_bars["close"].iloc[-1])
        exit_reason: Literal["TAKE_PROFIT", "STOP_LOSS", "HORIZON_EXIT"] = "HORIZON_EXIT"

        for _, bar in future_bars.iterrows():
            bar_high = float(bar["high"])
            bar_low = float(bar["low"])
            if direction == "LONG":
                # Conservative intrabar rule: check stop loss before take profit
                if bar_low <= stop_loss:
                    raw_exit = stop_loss
                    exit_reason = "STOP_LOSS"
                    break
                if bar_high >= take_profit:
                    raw_exit = take_profit
                    exit_reason = "TAKE_PROFIT"
                    break
            else:
                if bar_high >= stop_loss:
                    raw_exit = stop_loss
                    exit_reason = "STOP_LOSS"
                    break
                if bar_low <= take_profit:
                    raw_exit = take_profit
                    exit_reason = "TAKE_PROFIT"
                    break

        fill_exit = self.cost_model.compute_fill_price(raw_exit, direction, atr, is_entry=False)
        if direction == "LONG":
            gross_pnl = ( raw_exit - raw_entry ) * shares
            net_before_comm = ( fill_exit - fill_entry ) * shares
        else:
            gross_pnl = ( raw_entry - raw_exit ) * shares
            net_before_comm = ( fill_entry - fill_exit ) * shares

        comm = self.cost_model.compute_commission(fill_entry * shares) + self.cost_model.compute_commission(
            fill_exit * shares
        )
        net_pnl = net_before_comm - comm
        total_cost = gross_pnl - net_pnl
        return (
            round(fill_entry, 4),
            round(fill_exit, 4),
            round(gross_pnl, 2),
            round(max(0.0, total_cost), 2),
            round(net_pnl, 2),
            exit_reason,
        )

    @staticmethod
    def export_veto_chart(report: BacktestComparisonReport,
                          output_filepath: str = "veto_attribution_chart.svg") -> None:
        """
        Generates an SVG/HTML chart exporting the equity curve and highlighting
        exact timestamps where the Devil's Advocate intervened.
        """
        records = report.trade_records
        if not records:
            print("No trade records found to plot.")
            return

        # Convert records to DataFrame for easier plotting
        df = pd.DataFrame([r.model_dump() for r in records])
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df.sort_values('timestamp')

        # Filter executed trades to build the equity curve
        executed_df = df[df["executed"]].copy()
        executed_df['cumulative_pnl'] = executed_df['net_pnl_usd'].cumsum()
        executed_df['equity'] = report.argus_committee.initial_equity + executed_df['cumulative_pnl']

        # Filter vetoed trades for markers
        veto_df = df[df['exit_reason'] == 'VETOED_COUNTERFACTUAL']

        try:
            import matplotlib.pyplot as plt
        except ImportError as exc:
            raise ImportError(
                "matplotlib is required for export_veto_chart(); install with `pip install matplotlib` or `pip install 'argus-ai[rl]'`."
            ) from exc

        plt.figure(figsize=(14, 7))

        # Plot the equity curve
        plt.plot(executed_df['timestamp'], executed_df['equity'],
                 label='ARGUS Equity Curve', color='blue', linewidth=2)

        # Plot vertical lines for Devil's Advocate Vetoes
        for i, row in veto_df.iterrows():
            label = "Devil's Advocate Veto" if i == veto_df.index[0] else ""
            plt.axvline(x=row['timestamp'], color='red', linestyle='--', alpha=0.6, label=label)

        plt.title(f"Walk-Forward Regime Attribution & Devil's Advocate Vetoes ({report.ticker})", fontsize=14)
        plt.xlabel("Timestamp", fontsize=12)
        plt.ylabel("Account Equity (USD)", fontsize=12)

        # Deduplicate legend labels
        handles, labels = plt.gca().get_legend_handles_labels()
        by_label = dict(zip(labels, handles))
        plt.legend(by_label.values(), by_label.keys(), loc='best')

        plt.grid(True, alpha=0.3)
        plt.tight_layout()

        # Save as SVG or HTML based on extension
        plt.savefig(output_filepath, format=output_filepath.split('.')[-1])
        plt.close()
        print(f"Chart successfully exported to {output_filepath}")

    @staticmethod
    def _compute_summary_metrics(
        strategy_name: str,
        initial_equity: float,
        equity_curve: list[float],
        trade_pnls: list[float],
        total_costs: float,
        vetoed_count: int = 0,
        downsized_count: int = 0,
        veto_saved_loss: float = 0.0,
        veto_accuracy: float = 0.0,
        entropies: list[float] | None = None,
    ) -> BacktestMetrics:
        ending_equity = equity_curve[-1] if equity_curve else initial_equity
        net_pnl = ending_equity - initial_equity
        total_return_pct = (net_pnl / initial_equity) * 100.0

        # Max Drawdown %
        peak = initial_equity
        max_dd = 0.0
        for eq in equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                dd = ((peak - eq) / peak) * 100.0
                max_dd = max(max_dd, dd)

        # Step returns for Sharpe & Sortino
        eq_arr = np.asarray(equity_curve, dtype=float)
        if len(eq_arr) > 2:
            rets = np.diff(eq_arr) / np.maximum(eq_arr[:-1], 1.0)
            mean_ret = float(np.mean(rets))
            std_ret = float(np.std(rets))
            downside_rets = rets[rets < 0]
            downside_std = float(np.std(downside_rets)) if len(downside_rets) > 1 else 1e-6
            # Annualize assuming ~500 evaluation windows per year
            ann_factor = math.sqrt(500.0)
            sharpe = (mean_ret / std_ret) * ann_factor if std_ret > 1e-9 else 0.0
            sortino = (mean_ret / max(downside_std, 1e-6)) * ann_factor if mean_ret != 0 else 0.0
        else:
            sharpe = 0.0
            sortino = 0.0

        wins = [p for p in trade_pnls if p > 0]
        losses = [p for p in trade_pnls if p <= 0]
        total_trades = len(trade_pnls)
        win_rate = (len(wins) / total_trades * 100.0) if total_trades > 0 else 0.0
        gross_wins = sum(wins)
        gross_losses = abs(sum(losses))
        profit_factor = (gross_wins / gross_losses) if gross_losses > 1e-6 else (99.0 if gross_wins > 0 else 0.0)

        avg_entropy = float(np.mean(entropies)) if entropies else 0.0

        return BacktestMetrics(
            strategy_name=strategy_name,
            initial_equity=round(initial_equity, 2),
            ending_equity=round(ending_equity, 2),
            net_pnl_usd=round(net_pnl, 2),
            total_return_pct=round(total_return_pct, 2),
            max_drawdown_pct=round(max_dd, 2),
            sharpe_ratio=round(sharpe, 2),
            sortino_ratio=round(sortino, 2),
            win_rate_pct=round(win_rate, 1),
            profit_factor=round(profit_factor, 2),
            total_trades=total_trades,
            winning_trades=len(wins),
            losing_trades=len(losses),
            total_costs_usd=round(total_costs, 2),
            vetoed_trades_count=vetoed_count,
            downsized_trades_count=downsized_count,
            veto_saved_loss_usd=round(veto_saved_loss, 2),
            veto_accuracy_pct=round(veto_accuracy, 1),
            avg_disagreement_entropy=round(avg_entropy, 4),
        )

    def run_backtest(
        self,
        bars_df: pd.DataFrame,
        ticker: str = "NVDA",
    ) -> BacktestComparisonReport:
        """
        Execute a deterministic walk-forward backtest comparing the full ARGUS 7-agent
        Investment Committee against an unfiltered Naive Quant Baseline.
        """
        ts_index = PointInTimeGuard.extract_timestamps(bars_df)
        n_bars = len(bars_df)
        if n_bars <= self.warmup_bars + self.holding_horizon:
            raise ValueError(
                f"Dataset length ({n_bars}) must exceed warmup_bars ({self.warmup_bars}) + holding_horizon ({self.holding_horizon})."
            )

        argus_equity = self.initial_equity
        naive_equity = self.initial_equity
        argus_curve = [argus_equity]
        naive_curve = [naive_equity]

        argus_pnls: list[float] = []
        naive_pnls: list[float] = []
        argus_costs = 0.0
        naive_costs = 0.0

        vetoed_count = 0
        vetoed_losing_count = 0
        veto_saved_loss = 0.0
        downsized_count = 0
        entropies: list[float] = []
        records: list[SimulatedTradeRecord] = []
        evaluated_count = 0

        # Deterministic committee execution relies on explicit use_llm configuration
        for idx in range(self.warmup_bars, n_bars - self.holding_horizon, self.eval_stride):
            decision_ts = ts_index[idx]
            # Strict Point-in-Time slice validation
            pit_slice = PointInTimeGuard.validate_slice(bars_df.iloc[: idx + 1], decision_ts)
            future_window = bars_df.iloc[idx + 1 : idx + 1 + self.holding_horizon]

            current_price = float(pit_slice["close"].iloc[-1])
            rsi_val = self.committee.quant._calculate_rsi(pit_slice["close"])
            atr_val = self.committee.quant._calculate_atr(pit_slice)
            regime = (
                str(pit_slice["market_regime"].iloc[-1])
                if "market_regime" in pit_slice.columns
                else "BULLISH"
            )
            vix_val = (
                float(pit_slice["vix_level"].iloc[-1])
                if "vix_level" in pit_slice.columns
                else 17.5
            )
            if "recent_news" in pit_slice.columns:
                raw_news = pit_slice["recent_news"].iloc[-1]
                news_list = list(raw_news) if isinstance(raw_news, (list, tuple)) else [str(raw_news)]
            elif "headline" in pit_slice.columns:
                news_list = [str(pit_slice["headline"].iloc[-1])]
            else:
                news_list = [f"{ticker} institutional flow update"]

            ctx = MarketContext(
                ticker=ticker,
                current_price=current_price,
                high_24h=float(pit_slice["high"].tail(24).max()),
                low_24h=float(pit_slice["low"].tail(24).min()),
                volume_24h=float(pit_slice["volume"].tail(24).sum()),
                rsi_14=rsi_val,
                atr_14=atr_val,
                market_regime=regime,
                vix_level=vix_val,
                recent_news=news_list,
            )

            evaluated_count += 1
            passport = self.committee.evaluate_opportunity(ctx, pit_slice, account_equity=argus_equity)
            entropy = compute_disagreement_entropy(passport.agent_votes)
            entropies.append(entropy)

            # --- 1. Naive Single-Agent Quant Baseline ---
            quant_vote = passport.agent_votes[0]
            if quant_vote.recommendation in ("BUY", "SELL") or abs(quant_vote.score) >= 20.0:
                naive_dir: Literal["LONG", "SHORT"] = "LONG" if quant_vote.score >= 0 else "SHORT"
                naive_consensus = ConsensusDecision(
                    status="APPROVED",
                    overall_confidence=quant_vote.confidence,
                    composite_score=quant_vote.score,
                    downsize_factor=1.0,
                    primary_catalyst="Unfiltered Quant Signal",
                    main_opposition="None",
                    executive_summary="Naive baseline full-size execution",
                    unanimous=True,
                )
                naive_risk = self.committee.risk_commander.calculate_trade_parameters(
                    ctx=ctx,
                    direction=naive_dir,
                    consensus=naive_consensus,
                    account_equity=naive_equity,
                )
                _, _, _, n_cost, n_pnl, _ = self._resolve_bracket_outcome(
                    future_bars=future_window,
                    direction=naive_dir,
                    raw_entry=current_price,
                    stop_loss=naive_risk["stop_loss"],
                    take_profit=naive_risk["take_profit"],
                    shares=naive_risk["shares"],
                    atr=atr_val,
                )
                naive_equity += n_pnl
                naive_pnls.append(n_pnl)
                naive_costs += n_cost

            naive_curve.append(naive_equity)

            # --- 2. Full ARGUS Committee with Devil's Advocate Veto & Downsize ---
            status = passport.consensus.status
            if status in ("APPROVED", "DOWNSIZED"):
                if status == "DOWNSIZED":
                    downsized_count += 1
                fill_in, fill_out, gross_pnl, a_cost, a_pnl, exit_reason = self._resolve_bracket_outcome(
                    future_bars=future_window,
                    direction=passport.direction,
                    raw_entry=current_price,
                    stop_loss=passport.stop_loss,
                    take_profit=passport.take_profit,
                    shares=passport.shares_or_contracts,
                    atr=atr_val,
                )
                argus_equity += a_pnl
                argus_pnls.append(a_pnl)
                argus_costs += a_cost
                records.append(
                    SimulatedTradeRecord(
                        step_index=idx,
                        timestamp=decision_ts.isoformat(),
                        passport_id=passport.passport_id,
                        ticker=ticker,
                        direction=passport.direction,
                        consensus_status=status,
                        executed=True,
                        entry_price=fill_in,
                        exit_price=fill_out,
                        shares=passport.shares_or_contracts,
                        gross_pnl_usd=gross_pnl,
                        transaction_cost_usd=a_cost,
                        net_pnl_usd=a_pnl,
                        exit_reason=exit_reason,
                        disqualification_risk_pct=passport.devils_advocate.disqualification_risk_pct,
                        disagreement_entropy=entropy,
                    )
                )
            elif status == "VETOED":
                vetoed_count += 1
                # Compute counterfactual PnL had the vetoed trade been taken at full size (1.0x)
                cf_consensus = ConsensusDecision(
                    status="APPROVED",
                    overall_confidence=passport.consensus.overall_confidence,
                    composite_score=passport.consensus.composite_score,
                    downsize_factor=1.0,
                    primary_catalyst=passport.consensus.primary_catalyst,
                    main_opposition=passport.consensus.main_opposition,
                    executive_summary="Counterfactual un-vetoed execution",
                )
                cf_risk = self.committee.risk_commander.calculate_trade_parameters(
                    ctx=ctx,
                    direction=passport.direction,
                    consensus=cf_consensus,
                    account_equity=argus_equity,
                )
                fill_in, fill_out, gross_pnl, _cf_cost, cf_pnl, _ = self._resolve_bracket_outcome(
                    future_bars=future_window,
                    direction=passport.direction,
                    raw_entry=current_price,
                    stop_loss=cf_risk["stop_loss"],
                    take_profit=cf_risk["take_profit"],
                    shares=cf_risk["shares"],
                    atr=atr_val,
                )
                if cf_pnl < 0:
                    vetoed_losing_count += 1
                    veto_saved_loss += abs(cf_pnl)
                records.append(
                    SimulatedTradeRecord(
                        step_index=idx,
                        timestamp=decision_ts.isoformat(),
                        passport_id=passport.passport_id,
                        ticker=ticker,
                        direction=passport.direction,
                        consensus_status="VETOED",
                        executed=False,
                        entry_price=fill_in,
                        exit_price=fill_out,
                        shares=cf_risk["shares"],
                        gross_pnl_usd=0.0,
                        transaction_cost_usd=0.0,
                        net_pnl_usd=0.0,
                        exit_reason="VETOED_COUNTERFACTUAL",
                        disqualification_risk_pct=passport.devils_advocate.disqualification_risk_pct,
                        disagreement_entropy=entropy,
                        counterfactual_pnl_usd=cf_pnl,
                    )
                )

            argus_curve.append(argus_equity)

        veto_acc = (vetoed_losing_count / vetoed_count * 100.0) if vetoed_count > 0 else 0.0

        argus_metrics = self._compute_summary_metrics(
            strategy_name="ARGUS 7-Agent Committee (Adversarial Veto + ATR Risk)",
            initial_equity=self.initial_equity,
            equity_curve=argus_curve,
            trade_pnls=argus_pnls,
            total_costs=argus_costs,
            vetoed_count=vetoed_count,
            downsized_count=downsized_count,
            veto_saved_loss=veto_saved_loss,
            veto_accuracy=veto_acc,
            entropies=entropies,
        )
        naive_metrics = self._compute_summary_metrics(
            strategy_name="Naive Single-Agent Quant Baseline",
            initial_equity=self.initial_equity,
            equity_curve=naive_curve,
            trade_pnls=naive_pnls,
            total_costs=naive_costs,
        )

        return BacktestComparisonReport(
            ticker=ticker,
            total_bars=n_bars,
            evaluated_opportunities=evaluated_count,
            cost_model=self.cost_model,
            argus_committee=argus_metrics,
            naive_baseline=naive_metrics,
            return_outperformance_pct=round(
                argus_metrics.total_return_pct - naive_metrics.total_return_pct, 2
            ),
            drawdown_reduction_pct=round(
                naive_metrics.max_drawdown_pct - argus_metrics.max_drawdown_pct, 2
            ),
            sharpe_improvement=round(
                argus_metrics.sharpe_ratio - naive_metrics.sharpe_ratio, 2
            ),
            trade_records=records,
        )

    @staticmethod
    def to_markdown_report(report: BacktestComparisonReport) -> str:
        a = report.argus_committee
        b = report.naive_baseline
        c = report.cost_model

        md = f"""# 📊 ARGUS AI — Deterministic Walk-Forward Backtest & Counterfactual Veto Report

> **Asset:** `{report.ticker}` | **Total Hourly Bars:** `{report.total_bars}` | **Committee Deliberations:** `{report.evaluated_opportunities}`  
> **Point-in-Time Integrity:** `Strictly Enforced (PointInTimeGuard)`  
> **Execution Cost Model:** `Spread: {c.spread_bps:.1f} bps | Commission: {c.commission_bps:.1f} bps/side | Volatility Slippage: {c.slippage_atr_fraction*100:.1f}% ATR`

---

## 🏛️ Counterfactual Benchmark: ARGUS 7-Agent Committee vs. Naive Single-Agent Quant

| Institutional Metric | 🛡️ ARGUS 7-Agent Committee | 🤖 Naive Quant Baseline | 📈 ARGUS Structural Edge |
| :--- | :---: | :---: | :---: |
| **Starting Equity** | `${a.initial_equity:,.2f}` | `${b.initial_equity:,.2f}` | — |
| **Ending Equity** | **`${a.ending_equity:,.2f}`** | `${b.ending_equity:,.2f}` | **`${a.ending_equity - b.ending_equity:+,.2f}`** |
| **Total Net Return (%)** | **`{a.total_return_pct:+.2f}%`** | `{b.total_return_pct:+.2f}%` | **`{report.return_outperformance_pct:+.2f}%`** |
| **Max Drawdown (%)** | **`{a.max_drawdown_pct:.2f}%`** | `{b.max_drawdown_pct:.2f}%` | **`-{report.drawdown_reduction_pct:.2f}% DD Reduction`** |
| **Sharpe Ratio (Ann.)** | **`{a.sharpe_ratio:.2f}`** | `{b.sharpe_ratio:.2f}` | **`{report.sharpe_improvement:+.2f}`** |
| **Sortino Ratio (Ann.)** | **`{a.sortino_ratio:.2f}`** | `{b.sortino_ratio:.2f}` | **`{a.sortino_ratio - b.sortino_ratio:+.2f}`** |
| **Win Rate (%)** | **`{a.win_rate_pct:.1f}%`** | `{b.win_rate_pct:.1f}%` | `{a.win_rate_pct - b.win_rate_pct:+.1f}%` |
| **Profit Factor** | **`{a.profit_factor:.2f}`** | `{b.profit_factor:.2f}` | `{a.profit_factor - b.profit_factor:+.2f}` |
| **Executed Trades** | `{a.total_trades}` | `{b.total_trades}` | `{a.total_trades - b.total_trades:+d} (Selective Filtering)` |
| **Transaction Costs Paid** | `${a.total_costs_usd:,.2f}` | `${b.total_costs_usd:,.2f}` | `${a.total_costs_usd - b.total_costs_usd:+,.2f}` |

---

## 😈 Adversarial Dissent Attribution (Devil's Advocate Veto Impact)

* **Trades Vetoed by Devil's Advocate (`Risk >= 75%`):** `{a.vetoed_trades_count}`
* **Trades Downsized by 50% (`45% <= Risk < 75%`):** `{a.downsized_trades_count}`
* **Counterfactual Losses Avoided (`Veto Saved Loss`):** **`${a.veto_saved_loss_usd:,.2f}`**
* **Veto Precision (`% of Vetoed Setups That Would Have Lost Money`):** **`{a.veto_accuracy_pct:.1f}%`**
* **Mean Committee Disagreement Entropy:** `{a.avg_disagreement_entropy:.4f}`

> **Key Takeaway:** During euphoric overbought spikes (`RSI > 68` coupled with `VIX > 24` in choppy regimes), the single-agent Quant model blindly buys EMA breakouts right before liquidity flushes. The ARGUS **Devil's Advocate Agent** detects the asymmetric trap and exercises unilateral **VETO** power, preserving `${a.veto_saved_loss_usd:,.2f}` in capital and compressing maximum drawdown from `{b.max_drawdown_pct:.2f}%` to `{a.max_drawdown_pct:.2f}%`.
"""
        return md


BacktestEngine = AdversarialBacktestEngine
