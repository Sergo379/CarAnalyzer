import json
from typing import Any

import httpx

from backend.ai.base import AIProvider, AIProviderError, AIProviderNotConfiguredError


class YandexGPTProvider(AIProvider):
    endpoint = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

    def __init__(
        self,
        api_key: str | None = None,
        folder_id: str | None = None,
        model: str = "yandexgpt/latest",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.folder_id = folder_id
        self.model = model
        self.transport = transport

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.folder_id)

    def _require_credentials(self) -> tuple[str, str]:
        if not self.api_key or not self.folder_id:
            raise AIProviderNotConfiguredError("YandexGPT credentials are not configured")
        return self.api_key, self.folder_id

    async def _complete(self, prompt: str, json_object: bool = False) -> str:
        api_key, folder_id = self._require_credentials()
        payload: dict[str, Any] = {
            "modelUri": f"gpt://{folder_id}/{self.model}",
            "completionOptions": {"stream": False, "temperature": 0.1, "maxTokens": "2000"},
            "messages": [{"role": "user", "text": prompt}],
        }
        if json_object:
            payload["jsonObject"] = True
        async with httpx.AsyncClient(timeout=60, transport=self.transport) as client:
            response = await client.post(
                self.endpoint,
                headers={"Authorization": f"Api-Key {api_key}"},
                json=payload,
            )
        try:
            response.raise_for_status()
            return str(response.json()["result"]["alternatives"][0]["message"]["text"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise AIProviderError("YandexGPT completion failed") from exc

    async def analyze_car_problems(self, context: str) -> dict[str, Any]:
        text = await self._complete(
            "Return grounded JSON only with keys common_problems, problematic_components, "
            "inspection_points, expensive_failures, risk_summary. Use only this context:\n"
            + context,
            json_object=True,
        )
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise AIProviderError("YandexGPT returned invalid JSON") from exc

    async def classify_vehicle_segment(self, brand: str, model: str) -> str:
        return (
            await self._complete(
                f"Classify {brand} {model} using category, size class, positioning; concise JSON.",
                json_object=True,
            )
        ).strip()
