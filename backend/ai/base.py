from abc import ABC, abstractmethod
from typing import Any


class AIProviderError(RuntimeError):
    pass


class AIProviderNotConfiguredError(AIProviderError):
    pass


class AIProvider(ABC):
    @abstractmethod
    async def analyze_car_problems(self, context: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def classify_vehicle_segment(self, brand: str, model: str) -> str:
        raise NotImplementedError
