import json
from typing import Any
from uuid import uuid4

import httpx

from backend.ai.base import AIProvider, AIProviderError, AIProviderNotConfiguredError


class GigaChatProvider(AIProvider):
    oauth_endpoint = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    completion_endpoint = "https://api.giga.chat/v1/chat/completions"

    def __init__(
        self,
        credentials: str | None = None,
        model: str = "GigaChat",
        scope: str = "GIGACHAT_API_PERS",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.credentials = credentials
        self.model = model
        self.scope = scope
        self.transport = transport

    @property
    def is_configured(self) -> bool:
        return bool(self.credentials)

    async def _token(self, client: httpx.AsyncClient) -> str:
        if not self.credentials:
            raise AIProviderNotConfiguredError("GigaChat credentials are not configured")
        response = await client.post(
            self.oauth_endpoint,
            headers={"Authorization": f"Basic {self.credentials}", "RqUID": str(uuid4())},
            data={"scope": self.scope},
        )
        try:
            response.raise_for_status()
            return str(response.json()["access_token"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise AIProviderError("GigaChat authorization failed") from exc

    async def _complete(self, prompt: str) -> str:
        async with httpx.AsyncClient(timeout=60, transport=self.transport) as client:
            token = await self._token(client)
            response = await client.post(
                self.completion_endpoint,
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                },
            )
        try:
            response.raise_for_status()
            return str(response.json()["choices"][0]["message"]["content"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise AIProviderError("GigaChat completion failed") from exc

    async def analyze_car_problems(self, context: str) -> dict[str, Any]:
        text = await self._complete(
            "Верни только JSON с ключами common_problems, problematic_components, "
            "inspection_points, expensive_failures, risk_summary. Используй только контекст:\n"
            + context
        )
        text = text.removeprefix("```json").removesuffix("```").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise AIProviderError("GigaChat returned invalid JSON") from exc

    async def classify_vehicle_segment(self, brand: str, model: str) -> str:
        return (
            await self._complete(
                f"Классифицируй {brand} {model}: category, size_class, positioning. Кратко."
            )
        ).strip()
