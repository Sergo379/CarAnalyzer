from typing import Any

from backend.ai.base import AIProvider, AIProviderNotConfiguredError


class GigaChatProvider(AIProvider):
    """Credential boundary for the future official GigaChat integration."""

    def __init__(self, credentials: str | None = None) -> None:
        self.credentials = credentials

    def _require_credentials(self) -> str:
        if not self.credentials:
            raise AIProviderNotConfiguredError("GigaChat credentials are not configured")
        return self.credentials

    async def analyze_car_problems(self, context: str) -> dict[str, Any]:
        self._require_credentials()
        raise NotImplementedError("Live GigaChat calls require approved credentials")

    async def classify_vehicle_segment(self, brand: str, model: str) -> str:
        self._require_credentials()
        raise NotImplementedError("Live GigaChat calls require approved credentials")
