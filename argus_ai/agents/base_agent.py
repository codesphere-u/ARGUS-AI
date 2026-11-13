import json
import logging
from typing import Any

from argus_ai.config.settings import CONFIG

logger = logging.getLogger("argus.agents")


class BaseAgent:
    """Base class for ARGUS AI Committee Agents with Gemini structured inference."""

    def __init__(self, agent_name: str, use_llm: bool = True) -> None:
        self.name = agent_name
        self.use_llm = use_llm
        self.api_key = CONFIG.committee.gemini_api_key
        self.model_name = CONFIG.committee.model_name
        self._gemini_client: Any = None
        if self.use_llm:
            self._initialize_gemini()

    def _initialize_gemini(self) -> None:
        if not self.use_llm or not self.api_key:
            return
        try:
            from google import genai

            self._gemini_client = genai.Client(api_key=self.api_key)
        except Exception as e:  # noqa: BLE001 - optional external Gemini SDK boundary
            logger.debug("[%s] Could not initialize Gemini Client: %s", self.name, e)

    def query_llm_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any] | None:
        """Call Gemini model and parse structured JSON response."""
        if not self.use_llm or not self._gemini_client:
            return None

        try:
            response = self._gemini_client.models.generate_content(
                model=self.model_name,
                contents=(
                    f"System Instruction:\n{system_prompt}\n\n"
                    f"User Context:\n{user_prompt}\n\n"
                    "Respond ONLY with valid JSON conforming to the requested schema."
                ),
                config={
                    "response_mime_type": "application/json",
                    "temperature": 0.2,
                },
            )
            text = response.text
            if text:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
        except Exception as e:  # noqa: BLE001 - external LLM API fallback to deterministic heuristics
            logger.warning(
                "[%s] Gemini API call error (%s). Using specialized deterministic heuristic reasoning.",
                self.name,
                e,
            )
        return None
