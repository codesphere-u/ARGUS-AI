import json
import logging
import math
from pathlib import Path
from typing import Any, ClassVar

logger = logging.getLogger("argus.adaptive_weights")


class AdaptiveWeightManager:
    """
    Adaptive Committee Weight Manager:
    Continuously tracks agent prediction accuracy and attribution PnL.
    Dynamically recalibrates voting weights using Exponential Bayesian Updating.
    """

    DEFAULT_WEIGHTS: ClassVar[dict[str, float]] = {
        "Quant Agent": 0.35,
        "News & Sentiment Agent": 0.20,
        "Macro & Sector Agent": 0.20,
        "Options Flow Agent": 0.25,
    }

    def __init__(self, persistence_file: str = "argus_ai/data/adaptive_weights.json", learning_rate: float = 0.08) -> None:
        self.file_path = Path(persistence_file)
        self.learning_rate = learning_rate
        self.weights: dict[str, float] = self.DEFAULT_WEIGHTS.copy()
        self.track_record: dict[str, dict[str, Any]] = {
            agent: {"wins": 0, "losses": 0, "total_pnl": 0.0, "total_calls": 0}
            for agent in self.DEFAULT_WEIGHTS
        }
        self._load()

    def _load(self) -> None:
        if self.file_path.exists():
            try:
                with open(self.file_path, encoding="utf-8") as f:
                    data = json.load(f)
                    self.weights = data.get("weights", self.DEFAULT_WEIGHTS.copy())
                    self.track_record = data.get("track_record", self.track_record)
                logger.info("[ADAPTIVE WEIGHTS] Loaded weights: %s", self.weights)
            except (OSError, ValueError, TypeError) as e:
                logger.warning("[ADAPTIVE WEIGHTS] Error loading weights file (%s), using defaults.", e)

    def save(self) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "weights": self.weights,
                        "track_record": self.track_record,
                    },
                    f,
                    indent=2,
                )
        except OSError as e:
            logger.error("[ADAPTIVE WEIGHTS] Failed to save weights: %s", e)

    def get_weight(self, agent_name: str) -> float:
        return self.weights.get(agent_name, 0.25)

    def update_agent_performance(self, agent_name: str, trade_pnl: float, was_aligned: bool):
        """
        Update an individual agent's track record and recalibrate weights.
        - trade_pnl > 0 and was_aligned -> Reward
        - trade_pnl < 0 and was_aligned -> Penalty
        - trade_pnl > 0 and not was_aligned -> Penalty (missed opportunity)
        """
        if agent_name not in self.track_record:
            self.track_record[agent_name] = {"wins": 0, "losses": 0, "total_pnl": 0.0, "total_calls": 0}

        rec = self.track_record[agent_name]
        rec["total_calls"] += 1
        rec["total_pnl"] += trade_pnl if was_aligned else -trade_pnl

        if (trade_pnl > 0 and was_aligned) or (trade_pnl < 0 and not was_aligned):
            rec["wins"] += 1
            performance_delta = +1.0
        else:
            rec["losses"] += 1
            performance_delta = -1.0

        # Exponential weight multiplier
        current_w = self.weights.get(agent_name, 0.25)
        new_raw_w = current_w * math.exp(self.learning_rate * performance_delta)
        self.weights[agent_name] = max(0.05, min(0.60, new_raw_w))

        # Normalize weights to sum to 1.0
        total_w = sum(self.weights.values())
        if total_w > 0:
            self.weights = {k: round(v / total_w, 3) for k, v in self.weights.items()}

        self.save()
        logger.info("[ADAPTIVE WEIGHTS] Recalibrated weights after %s update: %s", agent_name, self.weights)
