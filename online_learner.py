"""Daily online PPO fine-tuning from the trade journal + recent market data."""

from __future__ import annotations

import logging
import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from config import BOT, MODELS_DIR, ONLINE, OnlineLearnConfig
from environment import TradingEnv
from macro_filter import MacroFilter
from trade_journal import TradeJournal, default_journal

from telegram_format import format_learn_complete, format_learn_error

logger = logging.getLogger(__name__)

try:
    from stable_baselines3 import PPO
except ImportError:  # pragma: no cover
    PPO = None  # type: ignore

_LAST_LEARN_DATE: date | None = None
_LAST_SMOKE_SHARPE: float | None = None


def _backup_path(model_path: Path) -> Path:
    return model_path.with_name(model_path.stem + ".prev" + model_path.suffix)


def _smoke_sharpe(model, df: pd.DataFrame, feature_cols: list[str], symbol: str) -> float:
    """Quick deterministic replay Sharpe on the last N bars."""
    n = min(ONLINE.smoke_bars, len(df))
    if n < 50:
        return 0.0
    slice_df = df.iloc[-n:].reset_index(drop=True)
    env = TradingEnv(
        slice_df,
        feature_cols,
        initial_balance=BOT.initial_balance,
        symbol=symbol,
        use_macro=True,
        enforce_macro_gate=True,
    )
    obs, _ = env.reset()
    equity = [BOT.initial_balance]
    done = False
    while not done:
        action, _ = model.predict(obs, deterministic=True)
        obs, _, terminated, truncated, info = env.step(int(action))
        done = terminated or truncated
        equity.append(info["equity"])
    eq = np.asarray(equity, dtype=float)
    rets = np.diff(eq) / np.maximum(eq[:-1], 1e-8)
    if rets.std() <= 0:
        return 0.0
    return float((rets.mean() / rets.std()) * np.sqrt(35040.0))


def run_daily_update(
    df: pd.DataFrame,
    feature_cols: list[str],
    *,
    model_path: Path | None = None,
    journal: TradeJournal | None = None,
    cfg: OnlineLearnConfig | None = None,
    symbol: str | None = None,
    force: bool = False,
    has_open_position: bool = False,
    notify: Any = None,
    max_steps: int | None = None,
) -> dict[str, Any]:
    """
    Fine-tune PPO on recent data if enough closed trades exist.

    Guardrails
    ----------
    - Skip if disabled / already ran today / open position / too few trades.
    - Backup model, fine-tune with low LR, rollback if smoke Sharpe collapses.
    - ``max_steps`` overrides cfg for micro live updates.
    """
    global _LAST_LEARN_DATE, _LAST_SMOKE_SHARPE

    cfg = cfg or ONLINE
    journal = journal or default_journal
    model_path = model_path or BOT.model_path
    symbol = symbol or BOT.symbol
    today = datetime.now(timezone.utc).date()
    steps = int(max_steps) if max_steps is not None else int(cfg.max_steps)

    result: dict[str, Any] = {"status": "skipped", "reason": ""}

    if not cfg.enabled and not force:
        result["reason"] = "ONLINE_LEARN_ENABLED=false"
        return result
    if has_open_position:
        result["reason"] = "Open position — defer learning"
        return result
    if _LAST_LEARN_DATE == today and not force:
        result["reason"] = "Already learned today"
        return result
    if PPO is None:
        result["reason"] = "stable-baselines3 not installed"
        return result
    if not model_path.exists():
        result["reason"] = f"No model at {model_path}"
        return result

    n_trades = journal.count_closed(days=cfg.lookback_days)
    if n_trades < cfg.min_trades and not force:
        result["reason"] = f"Need {cfg.min_trades} closed trades, have {n_trades}"
        return result
    if force and n_trades < 1:
        logger.warning("Forced learn with %s closed trades — using market data only.", n_trades)

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    backup = _backup_path(model_path)
    shutil.copy2(model_path, backup)
    logger.info("Backed up model to %s", backup)

    try:
        env = TradingEnv(
            df,
            feature_cols,
            initial_balance=BOT.initial_balance,
            symbol=symbol,
            use_macro=True,
            enforce_macro_gate=True,
            macro_filter=MacroFilter(),
        )
        model = PPO.load(str(model_path), env=env)
        model.learning_rate = cfg.learning_rate

        baseline = _smoke_sharpe(model, df, feature_cols, symbol)
        if _LAST_SMOKE_SHARPE is None:
            _LAST_SMOKE_SHARPE = baseline

        logger.info(
            "Online learn: %s steps @ lr=%s (closed_trades=%s, baseline_sharpe=%.3f)",
            steps,
            cfg.learning_rate,
            n_trades,
            baseline,
        )
        model.learn(total_timesteps=steps, reset_num_timesteps=False)

        tmp = model_path.with_suffix(".tmp.zip")
        model.save(str(tmp))
        tmp.replace(model_path)

        new_sharpe = _smoke_sharpe(model, df, feature_cols, symbol)
        collapse = (_LAST_SMOKE_SHARPE - new_sharpe) >= cfg.sharpe_collapse_delta
        if collapse and baseline - new_sharpe >= cfg.sharpe_collapse_delta:
            shutil.copy2(backup, model_path)
            result.update(
                {
                    "status": "rolled_back",
                    "reason": f"Sharpe collapsed {baseline:.3f} → {new_sharpe:.3f}",
                    "baseline_sharpe": baseline,
                    "new_sharpe": new_sharpe,
                }
            )
            logger.warning("Online learn rolled back: %s", result["reason"])
            if notify:
                notify(f"⚠️ Online learn rolled back: {result['reason']}")
            return result

        _LAST_LEARN_DATE = today
        _LAST_SMOKE_SHARPE = new_sharpe
        result.update(
            {
                "status": "updated",
                "reason": "ok",
                "steps": steps,
                "closed_trades": n_trades,
                "baseline_sharpe": baseline,
                "new_sharpe": new_sharpe,
            }
        )
        msg = format_learn_complete(
            steps=steps,
            trades=n_trades,
            baseline_sharpe=baseline,
            new_sharpe=new_sharpe,
        )
        logger.info(
            "Online learn complete steps=%s trades=%s sharpe %.3f → %.3f",
            steps,
            n_trades,
            baseline,
            new_sharpe,
        )
        if notify:
            notify(msg)
        return result
    except Exception as exc:  # noqa: BLE001
        if backup.exists():
            shutil.copy2(backup, model_path)
        result.update({"status": "error", "reason": str(exc)})
        logger.exception("Online learn failed: %s", exc)
        if notify:
            notify(format_learn_error(exc))
        return result


def maybe_daily_learn(
    df: pd.DataFrame,
    feature_cols: list[str],
    *,
    has_open_position: bool,
    notify: Any = None,
) -> dict[str, Any]:
    """Trigger once-per-day learning from the live loop."""
    return run_daily_update(
        df,
        feature_cols,
        has_open_position=has_open_position,
        notify=notify,
        force=False,
    )
