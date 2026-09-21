from abc import ABC, abstractmethod

from backend.models.car import SearchRequest
from backend.models.listing import CarListing


class BaseScraper(ABC):
    source: str

    @abstractmethod
    async def search(self, query: SearchRequest) -> list[CarListing]:
        raise NotImplementedError
