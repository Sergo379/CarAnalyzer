from backend.ai.base import AIProvider
from backend.ai.gigachat import GigaChatProvider
from backend.ai.yandex import YandexGPTProvider
from backend.config import Settings, get_settings


def provider_from_settings(name: str, settings: Settings | None = None) -> AIProvider:
    settings = settings or get_settings()
    if name.casefold() == "yandex":
        return YandexGPTProvider(
            settings.yandex_api_key,
            settings.yandex_folder_id,
            settings.yandex_model,
        )
    if name.casefold() == "gigachat":
        return GigaChatProvider(
            settings.gigachat_credentials,
            settings.gigachat_model,
            settings.gigachat_scope,
        )
    raise ValueError(f"Unknown AI provider: {name}")
