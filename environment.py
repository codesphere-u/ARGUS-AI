"""Custom Gymnasium trading environment for Forex RL training."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar

import numpy as np
import pandas as pd

try:
    import gymnasium as gym
    from gymnasium import spaces
except ImportError:
    class _FallbackEnv:
        metadata: ClassVar[dict[str, Any]] = {}

        def __init__(self) -> None:
            pass

        def reset(
            self,
            *,
            seed: int | None = None,
            options: dict[str, Any] | None = None,
        ) -> None:
            if seed is not None:
                np.random.seed(seed)

    class _FallbackBox:
        def __init__(
            self,
            low: float,
            high: float,
            shape: tuple[int, ...],
            dtype: Any = np.float32,
        ) -> None:
            self.low = low
            self.high = high
            self.shape = shape
            self.dtype = dtype

    class _FallbackDiscrete:
        def __init__(self, n: int) -> None:
            self.n = int(n)

        def contains(self, x: int) -> bool:
            return isinstance(x, (int, np.integer)) and 0 <= int(x) < self.n

    class _FallbackSpaces:
        Box = _FallbackBox
        Discrete = _FallbackDiscrete

    class _FallbackGym:
        Env = _FallbackEnv
        spaces = _FallbackSpaces

    gym = _FallbackGym()  # type: ignore[assignment]
    spaces = _FallbackSpaces()  # type: ignore[assignment]

from config import RISK, RiskConfig
from macro_filter import MACRO_FEATURE_KEYS, MacroFilter

# Number of macro extras appended to every observation
N_MACRO_FEATURES = len(MACRO_FEATURE_KEYS)


class TradingEnv(gym.Env):
    """
    Discrete-action Forex trading environment.

    State
    -----
    Normalized technical indicators + position + unrealized PnL + macro features
    (blackout, mins_to_event, london, ny, overlap).

    Actions
    -------
    0 = HOLD, 1 = BUY (long), 2 = SELL (close / short flat-to-flat close).

    Reward
    ------
    Differential equity return scaled by recent volatility (Sharpe-like),
    minus a modest drawdown penalty and fees.
    """

    metadata: ClassVar[dict[str, list[str]]] = {"render_modes": ["human"]}

    ACTION_HOLD = 0
    ACTION_BUY = 1
    ACTION_SELL = 2

    def __init__(
        self,
        df: pd.DataFrame,
        feature_cols: list[str],
        initial_balance: float = 10_000.0,
        risk_cfg: RiskConfig | None = None,
        atr_col: str = "atr_14",
        close_col: str = "close",
        macro_filter: MacroFilter | None = None,
        symbol: str = "EURUSDm",
        use_macro: bool = True,
        enforce_macro_gate: bool = False,
    ) -> None:
        super().__init__()
        if df.empty:
            raise ValueError("TradingEnv requires a non-empty feature DataFrame.")
        if not feature_cols:
            raise ValueError("feature_cols must be a non-empty list.")

        self.df = df.reset_index(drop=True)
        self.feature_cols = list(feature_cols)
        self.initial_balance = float(initial_balance)
        self.risk_cfg = risk_cfg or RISK
        self.atr_col = atr_col
        self.close_col = close_col
        self.macro_filter = macro_filter or MacroFilter()
        self.symbol = symbol
        self.use_macro = use_macro
        self.enforce_macro_gate = enforce_macro_gate

        n_features = len(self.feature_cols) + 2  # + position + unrealized PnL
        if self.use_macro:
            n_features += N_MACRO_FEATURES
        self.observation_space = spaces.Box(
            low=-10.0,
            high=10.0,
            shape=(n_features,),
            dtype=np.float32,
        )
        self.action_space = spaces.Discrete(3)

        self._step_idx = 0
        self.balance = self.initial_balance
        self.equity = self.initial_balance
        self.peak_equity = self.initial_balance
        self.position = 0  # 0 flat, 1 long
        self.entry_price = 0.0
        self.position_size = 0.0
        self.realized_pnl = 0.0
        self.trade_count = 0
        self.equity_curve: list[float] = []
        self._recent_returns: list[float] = []

    def _bar_time(self) -> datetime:
        if "time" in self.df.columns:
            ts = self.df.loc[self._step_idx, "time"]
            if isinstance(ts, datetime):
                return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            return pd.Timestamp(ts).to_pydatetime().replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc)

    def _macro_vec(self) -> np.ndarray:
        if not self.use_macro:
            return np.zeros(0, dtype=np.float32)
        vec = self.macro_filter.macro_feature_vector(self._bar_time(), self.symbol)
        return np.asarray(vec, dtype=np.float32)

    def _current_price(self) -> float:
        return float(self.df.loc[self._step_idx, self.close_col])

    def _current_atr(self) -> float:
        atr = float(self.df.loc[self._step_idx, self.atr_col])
        return max(atr, 1e-8)

    def _unrealized_pnl(self) -> float:
        if self.position == 0 or self.position_size <= 0:
            return 0.0
        return (self._current_price() - self.entry_price) * self.position_size

    def _normalized_unrealized(self) -> float:
        pnl = self._unrealized_pnl()
        return float(np.clip(pnl / max(self.initial_balance, 1.0), -5.0, 5.0))

    def _get_obs(self) -> np.ndarray:
        row = self.df.loc[self._step_idx, self.feature_cols].to_numpy(dtype=np.float32)
        row = np.nan_to_num(row, nan=0.0, posinf=0.0, neginf=0.0)
        extras = np.array(
            [float(self.position), self._normalized_unrealized()],
            dtype=np.float32,
        )
        parts = [row, extras]
        if self.use_macro:
            parts.append(self._macro_vec())
        return np.concatenate(parts)

    def _drawdown_penalty(self) -> float:
        dd = 0.0
        if self.peak_equity > 0:
            dd = max(0.0, (self.peak_equity - self.equity) / self.peak_equity)
        return dd

    def _position_notional(self) -> float:
        """Risk 1.5% of equity with SL distance = 1.5 * ATR."""
        risk_amount = self.equity * self.risk_cfg.max_risk_per_trade
        sl_distance = self.risk_cfg.stop_loss_atr_mult * self._current_atr()
        if sl_distance <= 0:
            return 0.0
        units = risk_amount / sl_distance
        return max(units, 0.0)

    def reset(
        self,
        *,
        seed: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        super().reset(seed=seed)
        self._step_idx = 0
        self.balance = self.initial_balance
        self.equity = self.initial_balance
        self.peak_equity = self.initial_balance
        self.position = 0
        self.entry_price = 0.0
        self.position_size = 0.0
        self.realized_pnl = 0.0
        self.trade_count = 0
        self.equity_curve = [self.equity]
        self._recent_returns = []
        return self._get_obs(), self._info()

    def _info(self) -> dict[str, Any]:
        return {
            "balance": self.balance,
            "equity": self.equity,
            "position": self.position,
            "realized_pnl": self.realized_pnl,
            "step": self._step_idx,
            "trades": self.trade_count,
        }

    def step(
        self,
        action: int,
        *,
        exit_price: float | None = None,
    ) -> tuple[np.ndarray, float, bool, bool, dict[str, Any]]:
        if not self.action_space.contains(action):
            raise ValueError(f"Invalid action {action}")

        # Optional hard gate during backtest/training replay
        if self.enforce_macro_gate and action == self.ACTION_BUY and self.position == 0:
            allowed, _ = self.macro_filter.is_trading_allowed(self._bar_time(), self.symbol)
            if not allowed:
                action = self.ACTION_HOLD

        fee = 0.0
        realized = 0.0
        price = self._current_price()

        if action == self.ACTION_BUY and self.position == 0:
            size = self._position_notional()
            if size > 0:
                fee = abs(price * size) * self.risk_cfg.transaction_fee
                self.position = 1
                self.entry_price = price
                self.position_size = size
                self.balance -= fee
                self.trade_count += 1

        elif action == self.ACTION_SELL and self.position == 1:
            fill = float(exit_price) if exit_price is not None else price
            gross = (fill - self.entry_price) * self.position_size
            fee = abs(fill * self.position_size) * self.risk_cfg.transaction_fee
            realized = gross - fee
            self.balance += gross - fee
            self.realized_pnl += realized
            self.position = 0
            self.entry_price = 0.0
            self.position_size = 0.0
            self.trade_count += 1

        prev_equity = self.equity
        self.equity = self.balance + self._unrealized_pnl()
        self.peak_equity = max(self.peak_equity, self.equity)
        self.equity_curve.append(self.equity)

        # Sharpe-like differential reward: step return / rolling vol − modest DD cost
        step_ret = (self.equity - prev_equity) / max(self.initial_balance, 1.0)
        self._recent_returns.append(step_ret)
        if len(self._recent_returns) > 64:
            self._recent_returns = self._recent_returns[-64:]
        vol = float(np.std(self._recent_returns)) if len(self._recent_returns) >= 8 else 0.0
        vol = max(vol, 1e-4)
        dd_penalty = self._drawdown_penalty()
        fee_term = fee / max(self.initial_balance, 1.0)
        reward = float((step_ret / vol) - (dd_penalty * 1.0) - fee_term)

        self._step_idx += 1
        terminated = self._step_idx >= len(self.df) - 1
        truncated = False

        if terminated and self.position == 1:
            price = self._current_price()
            gross = (price - self.entry_price) * self.position_size
            fee = abs(price * self.position_size) * self.risk_cfg.transaction_fee
            self.balance += gross - fee
            self.realized_pnl += gross - fee
            self.position = 0
            self.position_size = 0.0
            self.equity = self.balance

        obs = self._get_obs()
        return obs, reward, terminated, truncated, self._info()

    def render(self) -> None:
        print(
            f"step={self._step_idx} equity={self.equity:.2f} "
            f"pos={self.position} pnl={self.realized_pnl:.2f}"
        )


def build_live_observation(
    technical_features: np.ndarray,
    position_flag: float,
    unrealized_norm: float,
    macro_vec: list[float] | np.ndarray,
) -> np.ndarray:
    """Assemble observation vector matching ``TradingEnv._get_obs`` layout."""
    tech = np.nan_to_num(np.asarray(technical_features, dtype=np.float32), nan=0.0)
    extras = np.array([position_flag, unrealized_norm], dtype=np.float32)
    macro = np.asarray(macro_vec, dtype=np.float32)
    return np.concatenate([tech, extras, macro])
