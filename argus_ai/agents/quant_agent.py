import pandas as pd
import numpy as np
from typing import Dict, Any, List
from argus_ai.agents.base_agent import BaseAgent
from argus_ai.core.models import AgentVote, MarketContext


class QuantAgent(BaseAgent):
    """
    Quant Agent: Evaluates price action, volatility, technical indicators,
    momentum, and liquidity to produce deterministic mathematical signals.
    """

    def __init__(self):
        super().__init__("Quant Agent")

    def analyze(self, ctx: MarketContext, bars_df: pd.DataFrame) -> AgentVote:
        # Calculate technical indicators
        rsi = self._calculate_rsi(bars_df["close"])
        ema_20 = float(bars_df["close"].ewm(span=20).mean().iloc[-1])
        ema_50 = float(bars_df["close"].ewm(span=50).mean().iloc[-1])
        atr = self._calculate_atr(bars_df)
        
        current_price = ctx.current_price
        trend_bullish = current_price > ema_20 > ema_50
        trend_bearish = current_price < ema_20 < ema_50

        # LLM prompt
        system_prompt = (
            "You are the Lead Quantitative & Technical Analyst for ARGUS AI hedge fund. "
            "Analyze market data and indicators. Output JSON with fields: "
            "recommendation ('BUY', 'SELL', 'HOLD'), confidence (0.0 to 1.0), "
            "score (-100 to 100), key_arguments (list of strings), concerns_raised (list of strings)."
        )
        user_prompt = (
            f"Asset: {ctx.ticker}\n"
            f"Current Price: ${current_price:.2f}\n"
            f"RSI(14): {rsi:.2f}\n"
            f"EMA(20): ${ema_20:.2f}, EMA(50): ${ema_50:.2f}\n"
            f"ATR(14): ${atr:.2f}\n"
            f"24h Volume: {ctx.volume_24h:,.0f}\n"
        )

        llm_res = self.query_llm_json(system_prompt, user_prompt)
        if llm_res:
            try:
                return AgentVote(
                    agent_name=self.name,
                    recommendation=llm_res.get("recommendation", "HOLD"),
                    confidence=float(llm_res.get("confidence", 0.7)),
                    score=float(llm_res.get("score", 0.0)),
                    key_arguments=llm_res.get("key_arguments", []),
                    concerns_raised=llm_res.get("concerns_raised", []),
                    metrics={"rsi": rsi, "ema_20": ema_20, "ema_50": ema_50, "atr": atr}
                )
            except Exception:
                pass

        # Deterministic Heuristic Engine
        score = 0.0
        recs: List[str] = []
        concerns: List[str] = []

        if trend_bullish:
            score += 45.0
            recs.append(f"Bullish EMA alignment: Price (${current_price:.2f}) > EMA20 (${ema_20:.2f}) > EMA50 (${ema_50:.2f})")
        elif trend_bearish:
            score -= 45.0
            recs.append(f"Bearish EMA alignment: Price (${current_price:.2f}) < EMA20 (${ema_20:.2f}) < EMA50 (${ema_50:.2f})")
        else:
            recs.append(f"Price consolidating between EMA20 (${ema_20:.2f}) and EMA50 (${ema_50:.2f})")

        # RSI Momentum
        if rsi < 30:
            score += 35.0
            recs.append(f"RSI oversold ({rsi:.1f}) indicating potential mean-reversion bounce")
        elif rsi > 70:
            score -= 30.0
            concerns.append(f"RSI overbought ({rsi:.1f}) - elevated risk of momentum exhaustion")
        elif 45 <= rsi <= 68 and trend_bullish:
            score += 20.0
            recs.append(f"Healthy bullish momentum on RSI ({rsi:.1f})")
        elif 32 <= rsi <= 55 and trend_bearish:
            score -= 20.0
            recs.append(f"Healthy bearish momentum on RSI ({rsi:.1f})")

        if score >= 35.0:
            action = "BUY"
            conf = min(0.92, 0.50 + abs(score) / 200.0)
        elif score <= -35.0:
            action = "SELL"
            conf = min(0.92, 0.50 + abs(score) / 200.0)
        else:
            action = "HOLD"
            conf = 0.50

        return AgentVote(
            agent_name=self.name,
            recommendation=action,
            confidence=round(conf, 2),
            score=round(score, 1),
            key_arguments=recs,
            concerns_raised=concerns,
            metrics={"rsi": round(rsi, 2), "ema_20": round(ema_20, 2), "ema_50": round(ema_50, 2), "atr": round(atr, 2)}
        )

    def _calculate_rsi(self, series: pd.Series, period: int = 14) -> float:
        if len(series) < period + 1:
            return 50.0
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / (loss + 1e-9)
        rsi = 100 - (100 / (1 + rs))
        return float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0

    def _calculate_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        if len(df) < period:
            return float(df["close"].iloc[-1] * 0.015)
        high = df["high"]
        low = df["low"]
        close_prev = df["close"].shift(1)
        tr = pd.concat([high - low, (high - close_prev).abs(), (low - close_prev).abs()], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean().iloc[-1]
        return float(atr) if not pd.isna(atr) else float(df["close"].iloc[-1] * 0.015)
