from typing import Any

from backend.ai.base import AIProvider, AIProviderNotConfiguredError


class YandexGPTProvider(AIProvider):
    """Credential boundary for the future official YandexGPT integration."""

    def __init__(self, api_key: str | None = None, folder_id: str | None = None) -> None:
        self.api_key = api_key
        self.folder_id = folder_id

    def _require_credentials(self) -> tuple[str, str]:
        if not self.api_key or not self.folder_id:
            raise AIProviderNotConfiguredError("YandexGPT credentials are not configured")
        return self.api_key, self.folder_id

    async def analyze_car_problems(self, context: str) -> dict[str, Any]:
        self._require_credentials()
        raise NotImplementedError("Live YandexGPT calls require approved credentials")

    async def classify_vehicle_segment(self, brand: str, model: str) -> str:
        self._require_credentials()
        raise NotImplementedError("Live YandexGPT calls require approved credentials")
