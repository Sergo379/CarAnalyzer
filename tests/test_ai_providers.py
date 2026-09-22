import asyncio

import pytest

from backend.ai.base import AIProviderNotConfiguredError
from backend.ai.gigachat import GigaChatProvider
from backend.ai.yandex import YandexGPTProvider


@pytest.mark.parametrize("provider", [GigaChatProvider(), YandexGPTProvider()])
def test_unconfigured_ai_provider_fails_explicitly(provider: object) -> None:
    with pytest.raises(AIProviderNotConfiguredError):
        asyncio.run(provider.analyze_car_problems("grounded context"))  # type: ignore[attr-defined]
