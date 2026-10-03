"""Optional Langfuse tracing without coupling pipeline correctness to telemetry."""

from contextlib import nullcontext
from typing import Any

from backend.config import Settings, get_settings


class _NoopSpan:
    def update(self, **kwargs: Any) -> None:
        pass


class LangfuseObserver:
    def __init__(self, settings: Settings | None = None, client: Any = None) -> None:
        self.settings = settings or get_settings()
        self._client = client

    @property
    def enabled(self) -> bool:
        return self._client is not None or bool(
            self.settings.langfuse_public_key and self.settings.langfuse_secret_key
        )

    def span(
        self,
        name: str,
        *,
        input: dict[str, object] | None = None,
        as_type: str = "span",
        model: str | None = None,
    ):
        if not self.enabled:
            return nullcontext(_NoopSpan())
        if self._client is None:
            try:
                from langfuse import Langfuse
            except ImportError as exc:
                raise RuntimeError("Install the 'lab' extra for Langfuse tracing") from exc
            self._client = Langfuse(
                public_key=self.settings.langfuse_public_key,
                secret_key=self.settings.langfuse_secret_key,
                base_url=self.settings.langfuse_host,
            )
        kwargs = {"as_type": as_type, "name": name, "input": input}
        if model:
            kwargs["model"] = model
        return self._client.start_as_current_observation(**kwargs)
