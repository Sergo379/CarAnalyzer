"""Print non-secret technical-provider configuration status."""

from backend.config import get_settings
from backend.knowledge.gemini import GeminiProvider

if __name__ == "__main__":
    settings = get_settings()
    print(f"Gemini provider configured: {GeminiProvider(settings).configured}")
    print(f"Gemini model: {settings.gemini_model}")
    print("Gemini API key: never displayed")
